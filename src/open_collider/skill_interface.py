"""Skill interface — prepare prompts and parse responses for Claude Code orchestration.

The actual LLM calls are made by Claude Code (the skill), not by Python.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import yaml

from open_collider.config import load_project_config
from open_collider.phases.idea_generator import IdeaGenerator, sample_combos
from open_collider.phases.idea_scorer import IdeaScorer, apply_threshold, BATCH_SIZE
from open_collider.prompt_resolver import PromptResolver
from open_collider.scoring.data_loader import DataLoader
from open_collider.strategies.fresh import FreshStrategy, parse_domain_response
from open_collider.strategies.deepen import DeepenStrategy
from open_collider.strategies.refresh import RefreshStrategy

logger = logging.getLogger(__name__)


def _save_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ======================================================================
# STATE MANAGEMENT
# ======================================================================

def list_brainstorms(project_dir: str) -> list[dict]:
    """List all brainstorm sessions for a project."""
    brainstorms_dir = Path(project_dir) / "brainstorms"
    if not brainstorms_dir.is_dir():
        return []
    results = []
    for entry in sorted(brainstorms_dir.iterdir()):
        if entry.is_dir() and entry.name.startswith("brainstorm_"):
            iters = [d for d in entry.iterdir() if d.is_dir() and d.name.startswith("iter_")]
            loved_path = entry / "loved_ideas.json"
            liked_path = entry / "liked_ideas.json"
            n_loved = len(json.loads(loved_path.read_text())) if loved_path.is_file() else 0
            n_liked = len(json.loads(liked_path.read_text())) if liked_path.is_file() else 0
            results.append({
                "brainstorm_id": entry.name,
                "iterations": len(iters),
                "loved": n_loved,
                "liked": n_liked,
            })
    return results


def start_new_brainstorm(project_dir: str) -> str:
    """Reset state and create a new brainstorm directory."""
    project_path = Path(project_dir)
    brainstorms_dir = project_path / "brainstorms"
    brainstorms_dir.mkdir(parents=True, exist_ok=True)
    max_n = 0
    for entry in brainstorms_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("brainstorm_"):
            try:
                n = int(entry.name.split("_")[1])
                max_n = max(max_n, n)
            except (ValueError, IndexError):
                pass
    new_id = f"brainstorm_{max_n + 1:03d}"
    (brainstorms_dir / new_id).mkdir()
    # Reset state
    state = _make_fresh_state(new_id)
    _save_state(project_path, state)
    return new_id


def init_iteration(project_dir: str, brainstorm_id: str | None = None) -> dict:
    """Initialize an iteration. Returns all state needed by the skill."""
    project_path = Path(project_dir)
    config = load_project_config(project_dir)
    state = _load_state(project_path)

    if brainstorm_id:
        brainstorm_dir = project_path / "brainstorms" / brainstorm_id
        if not brainstorm_dir.is_dir():
            raise FileNotFoundError(
                f"Brainstorm '{brainstorm_id}' not found in {project_path / 'brainstorms'}"
            )
        state["brainstorm_id"] = brainstorm_id
        # Recalculate current_iteration from the actual brainstorm dir
        existing_iters = [
            d for d in brainstorm_dir.iterdir()
            if d.is_dir() and d.name.startswith("iter_")
        ]
        state["current_iteration"] = len(existing_iters)
        _save_state(project_path, state)

    if not state.get("brainstorm_id"):
        start_new_brainstorm(project_dir)
        state = _load_state(project_path)

    brainstorm_dir = project_path / "brainstorms" / state["brainstorm_id"]
    iteration = state["current_iteration"] + 1
    iter_dir = brainstorm_dir / f"iter_{iteration:03d}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    brief = _load_brief(project_path)
    text_bank = _load_text_bank(project_path)
    domain_history = _load_domain_history(brainstorm_dir)
    loved, liked = _load_loved_liked(brainstorm_dir)

    return {
        "iteration": iteration,
        "brainstorm_dir": str(brainstorm_dir),
        "iter_dir": str(iter_dir),
        "brief": brief,
        "text_bank": text_bank,
        "domain_history": domain_history,
        "loved_ideas": loved,
        "liked_ideas": liked,
        "config": config,
        "has_loved": len(loved) > 0,
        "has_liked": len(liked) > 0,
    }


# ======================================================================
# DOMAIN GENERATION
# ======================================================================

def prepare_domain_prompt(strategy: str, project_dir: str, state: dict) -> dict | None:
    """Build a domain generation prompt."""
    config = state["config"]
    brief = state["brief"]
    domain_history = state["domain_history"]
    loved = state["loved_ideas"]
    liked = state["liked_ideas"]

    if strategy == "fresh":
        result = FreshStrategy().build_prompt(domain_history, brief, config)
    elif strategy == "deepen":
        result = DeepenStrategy().build_prompt(loved, domain_history, brief, config)
    elif strategy == "refresh":
        result = RefreshStrategy().build_prompt(loved, liked, brief, config)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    if result is None:
        return None
    return {"prompt": result["prompt"], "model": result["model"], "strategy": strategy}


def parse_domain_response_text(response: str) -> str:
    """Parse LLM domain response into validated YAML string."""
    return parse_domain_response(response)


# ======================================================================
# IDEA GENERATION
# ======================================================================

def prepare_idea_prompts(
    project_dir: str,
    domain_bank_yaml: str,
    strategy_name: str,
    state: dict,
) -> list[dict]:
    """Build all idea generation prompts for one strategy."""
    project_path = Path(project_dir)
    config = state["config"]
    iter_dir = Path(state["iter_dir"])
    domains_dir = iter_dir / "domains"
    domains_dir.mkdir(parents=True, exist_ok=True)
    (domains_dir / f"{strategy_name}.yaml").write_text(domain_bank_yaml, encoding="utf-8")

    domain_bank = yaml.safe_load(domain_bank_yaml) or {}
    text_bank = state["text_bank"]

    data_loader = DataLoader(
        base_dir=str(Path(__file__).resolve().parent / "data"),
        project_dir=project_path,
        domain_bank_data=domain_bank,
    )

    prompt_resolver = PromptResolver(project_path)
    gen = IdeaGenerator(config, prompt_resolver)

    # Determine combo count
    strategies_cfg = config.get("strategies", {})
    is_first = state["iteration"] == 1 and not state["has_loved"]
    n_combos = config.get("combos_first_iteration", 24) if is_first else strategies_cfg.get(strategy_name, {}).get("combos", 12)

    text_ids = list(text_bank.get("text_inputs", {}).keys())
    set_ids = list(domain_bank.get("sets", {}).keys())
    stratified = config.get("stratified_sampling", True)
    all_combos = sample_combos(text_ids, set_ids, n_combos, stratified=stratified)

    model = config.get("generation_model", "claude-sonnet-4-20250514")

    strategy_dir = iter_dir / f"strategy_{strategy_name}"
    strategy_dir.mkdir(parents=True, exist_ok=True)

    prompts = []
    for t_id, s_id in all_combos:
        combo = f"{t_id}_{strategy_name}_{s_id}"
        collision_id = f"{combo}_{uuid.uuid4().hex[:8]}"
        cell_dir = strategy_dir / collision_id
        cell_dir.mkdir(parents=True, exist_ok=True)

        prompt = gen.assemble_prompt(t_id, s_id, data_loader)

        # Save prompt for reproducibility
        (cell_dir / "prompt.md").write_text(prompt, encoding="utf-8")

        prompts.append({
            "combo_id": combo,
            "collision_id": collision_id,
            "prompt": prompt,
            "model": model,
            "text_id": t_id,
            "set_id": s_id,
            "cell_dir": str(cell_dir),
        })
    return prompts


def parse_idea_response(combo_info: dict, response: str) -> list[dict]:
    """Parse one combo's LLM response into idea dicts."""
    combo = combo_info["combo_id"]
    collision_id = combo_info["collision_id"]

    # Save raw response for reproducibility
    cell_dir = combo_info.get("cell_dir")
    if cell_dir:
        cell_path = Path(cell_dir)
        cell_path.mkdir(parents=True, exist_ok=True)
        model_short = combo_info.get("model", "unknown").split("-")[0]
        (cell_path / f"response_{model_short}.md").write_text(response, encoding="utf-8")

    gen = IdeaGenerator({}, None)
    ideas = gen.parse_response(response, combo)

    for idea in ideas:
        idea["collision_id"] = collision_id
        idea["idea_id"] = f"{collision_id}_{idea['idea_num']}"
        idea["gen_model"] = combo_info.get("model", "")

    return ideas


# ======================================================================
# SCORING
# ======================================================================

def prepare_scoring_prompts(ideas: list[dict], project_dir: str, state: dict) -> list[dict]:
    """Build scoring prompts in batches."""
    config = state["config"]
    project_path = Path(project_dir)
    prompt_resolver = PromptResolver(project_path)
    scorer = IdeaScorer(config, prompt_resolver)

    judge_config = _load_judge_config(project_path)
    ref_high = judge_config.get("ref_high", judge_config.get("ref_haute", []))
    ref_low = judge_config.get("ref_low", judge_config.get("ref_basse", []))

    # Global renumbering
    global_ideas = []
    for idx, idea in enumerate(ideas, 1):
        gi = dict(idea)
        gi["_global_num"] = idx
        gi["_orig_idea_num"] = idea["idea_num"]
        global_ideas.append(gi)

    batch_size = config.get("scoring_batch_size", BATCH_SIZE)
    batches = [global_ideas[i:i + batch_size] for i in range(0, len(global_ideas), batch_size)]
    model = config.get("scoring_model", "claude-sonnet-4-20250514")

    prompts = []
    for batch_id, batch in enumerate(batches):
        prompt = scorer.assemble_prompt(batch, ref_high, ref_low)
        prompts.append({
            "batch_id": batch_id,
            "prompt": prompt,
            "model": model,
            "ideas_in_batch": batch,
        })
    return prompts


def parse_scoring_response(batch_info: dict, response: str, config: dict) -> list[dict]:
    """Parse one scoring batch. Recalculates aggregate. Does NOT set retained."""
    scorer = IdeaScorer(config)
    return scorer.parse_response(response, batch_info["ideas_in_batch"])


# ======================================================================
# FINALIZATION
# ======================================================================

def finalize_iteration(
    project_dir: str,
    state: dict,
    strategy_domain_yamls: dict[str, str],
    all_ideas: list[dict],
    scored_ideas: list[dict],
    strategy_to_ideas: dict[str, list[dict]],
) -> dict:
    """Save all results, update state, generate REPORT.md."""
    project_path = Path(project_dir)
    config = state["config"]
    brainstorm_dir = Path(state["brainstorm_dir"])
    iter_dir = Path(state["iter_dir"])
    iteration = state["iteration"]

    # NOTE: caller is responsible for calling apply_threshold() before this function.
    # scored_ideas should already have 'retained' field set.

    # Save scored ideas
    _save_json(iter_dir / "scored_ideas.json", scored_ideas)

    retained = [i for i in scored_ideas if i.get("retained")]

    # Update domain history (fresh only)
    if "fresh" in strategy_domain_yamls:
        _update_domain_history(brainstorm_dir, strategy_domain_yamls["fresh"])

    # Build strategies detail
    strategies_detail = {}
    for strat_name, yaml_str in strategy_domain_yamls.items():
        bank = yaml.safe_load(yaml_str) or {}
        n_ideas = len(strategy_to_ideas.get(strat_name, []))
        strategies_detail[strat_name] = {
            "n_sets": len(bank.get("sets", {})),
            "n_ideas": n_ideas,
        }

    # Save iter config with effective config snapshot
    from open_collider.phases.idea_scorer import DEFAULT_WEIGHTS
    _save_json(iter_dir / "config.json", {
        "iteration": iteration,
        "strategies_used": list(strategy_domain_yamls.keys()),
        "strategies_detail": strategies_detail,
        "ideas_generated": len(all_ideas),
        "ideas_retained": len(retained),
        "timestamp": datetime.now().isoformat(),
        "effective_config": {
            "scoring_axes": config.get("judge_axes", DEFAULT_WEIGHTS),
            "score_threshold": config.get("score_threshold"),
            "models": {
                "domain": config.get("domain_model"),
                "generation": config.get("generation_model"),
                "scoring": config.get("scoring_model"),
            },
        },
    })

    # Update brainstorm state
    bs_state = _load_state(project_path)
    bs_state["current_iteration"] = iteration
    bs_state["status"] = "awaiting_curation"
    bs_state["total_ideas_generated"] = bs_state.get("total_ideas_generated", 0) + len(all_ideas)
    bs_state["last_activity"] = datetime.now().isoformat()
    _save_state(project_path, bs_state)

    # Generate/update REPORT.md
    generate_report(project_dir, state)

    return {
        "iteration": iteration,
        "ideas_generated": len(all_ideas),
        "ideas_retained": len(retained),
        "strategies_detail": strategies_detail,
    }


def apply_flags(project_dir: str, iteration: int, flags: dict) -> None:
    """Save flags and rebuild loved/liked stores. Sets status to 'ready'."""
    project_path = Path(project_dir)
    bs_state = _load_state(project_path)
    brainstorm_dir = project_path / "brainstorms" / bs_state["brainstorm_id"]
    iter_dir = brainstorm_dir / f"iter_{iteration:03d}"

    _save_json(iter_dir / "flags.json", flags)

    # Rebuild loved/liked from ALL iterations' flags + scored_ideas
    loved, liked = [], []
    for idir in sorted(brainstorm_dir.iterdir()):
        if not idir.is_dir() or not idir.name.startswith("iter_"):
            continue
        flags_path = idir / "flags.json"
        scored_path = idir / "scored_ideas.json"
        if not flags_path.is_file() or not scored_path.is_file():
            continue
        iter_flags = json.loads(flags_path.read_text())
        scored = json.loads(scored_path.read_text())
        id_to_idea = {i["idea_id"]: i for i in scored}
        for idea_id, flag in iter_flags.items():
            idea = id_to_idea.get(idea_id)
            if not idea:
                continue
            if flag in ("loved", "love"):
                loved.append(idea)
            elif flag in ("liked", "like"):
                liked.append(idea)

    _save_json(brainstorm_dir / "loved_ideas.json", loved)
    _save_json(brainstorm_dir / "liked_ideas.json", liked)

    bs_state["status"] = "ready"
    bs_state["total_loved"] = len(loved)
    bs_state["total_liked"] = len(liked)
    bs_state["last_activity"] = datetime.now().isoformat()
    _save_state(project_path, bs_state)

    # Generate iteration report
    generate_iter_report(project_dir, iteration)


def mark_curated(project_dir: str) -> None:
    """Set status to awaiting_flags after curation."""
    project_path = Path(project_dir)
    bs_state = _load_state(project_path)
    bs_state["status"] = "awaiting_flags"
    bs_state["last_activity"] = datetime.now().isoformat()
    _save_state(project_path, bs_state)


def generate_iter_report(project_dir: str, iteration: int) -> str:
    """Generate iter_NNN/ITER_REPORT.md — all curated ideas with their flags.

    Called after flags are applied. Shows every curated idea with its flag status.
    """
    project_path = Path(project_dir)
    bs_state = _load_state(project_path)
    brainstorm_dir = project_path / "brainstorms" / bs_state.get("brainstorm_id", "")
    iter_dir = brainstorm_dir / f"iter_{iteration:03d}"

    curated_path = iter_dir / "curated_ideas.json"
    insights_path = iter_dir / "insights_without_collision.json"
    flags_path = iter_dir / "flags.json"
    config_path = iter_dir / "config.json"

    curated = json.loads(curated_path.read_text()) if curated_path.is_file() else []
    insights = json.loads(insights_path.read_text()) if insights_path.is_file() else []
    flags = json.loads(flags_path.read_text()) if flags_path.is_file() else {}
    iter_cfg = json.loads(config_path.read_text()) if config_path.is_file() else {}

    lines = [f"# Iteration {iteration} Report", ""]
    strategies = ", ".join(iter_cfg.get("strategies_used", []))
    lines.append(f"**Strategies:** {strategies}")
    lines.append(
        f"**Generated:** {iter_cfg.get('ideas_generated', '?')} | "
        f"**Retained:** {iter_cfg.get('ideas_retained', '?')} | "
        f"**Curated:** {len(curated)} | "
        f"**Insights without collision:** {len(insights)}"
    )

    n_loved = sum(1 for v in flags.values() if v in ("loved", "love"))
    n_liked = sum(1 for v in flags.values() if v in ("liked", "like"))
    n_trashed = len(flags) - n_loved - n_liked
    lines.append(f"**Flags:** {n_loved} loved, {n_liked} liked, {n_trashed} trashed")

    fb_path = iter_dir / "feedback.txt"
    if fb_path.is_file():
        lines.append(f"\n**Feedback:** {fb_path.read_text(encoding='utf-8').strip()}")

    lines.append("\n---\n")

    if curated:
        lines.append(f"## Curated Ideas ({len(curated)})")
        lines.append("")
        for c in curated:
            idea_id = c.get("idea_id", "")
            flag = flags.get(idea_id, "unflagged")
            flag_label = {"loved": "❤️ LOVED", "liked": "👍 LIKED", "trashed": "🗑️ TRASHED"}.get(flag, "")

            lines.append(f"### #{c.get('rank', '?')} [{c.get('score', '?')}] {flag_label}")
            lines.append(f"\n{c.get('text', '')}")
            if c.get("why_selected"):
                lines.append(f"\n**Why selected:** {c['why_selected']}")
            if c.get("source_note"):
                lines.append(f"**Source:** {c['source_note']}")
            if c.get("challenge"):
                lines.append(f"**Challenge:** {c['challenge']}")
            lines.append("")

    if insights:
        lines.append("---\n")
        lines.append(f"## Insights Without Collision ({len(insights)})")
        lines.append("")
        lines.append("*Curator pass 2: high-signal observations that did not arise from a true bisociation but are still worth surfacing.*")
        lines.append("")
        for c in insights:
            idea_id = c.get("idea_id", "")
            flag = flags.get(idea_id, "unflagged")
            flag_label = {"loved": "❤️ LOVED", "liked": "👍 LIKED", "trashed": "🗑️ TRASHED"}.get(flag, "")

            lines.append(f"### #{c.get('rank', '?')} [{c.get('score', '?')}] {flag_label}")
            lines.append(f"\n{c.get('text', '')}")
            if c.get("why_kept"):
                lines.append(f"\n**Why kept:** {c['why_kept']}")
            lines.append("")

    report = "\n".join(lines)
    (iter_dir / "ITER_REPORT.md").write_text(report, encoding="utf-8")
    return report


def generate_brainstorm_report(project_dir: str) -> str:
    """Generate brainstorm_NNN/REPORT.md — aggregated across all iterations.

    Called when closing the session. Loved/liked ideas on top, trashed below.
    """
    project_path = Path(project_dir)
    bs_state = _load_state(project_path)
    brainstorm_dir = project_path / "brainstorms" / bs_state.get("brainstorm_id", "")

    # Collect data from all iterations
    iter_summaries = []
    all_loved = []
    all_liked = []
    all_trashed = []
    all_insights = []
    all_feedback = []

    for idir in sorted(brainstorm_dir.iterdir()):
        if not idir.is_dir() or not idir.name.startswith("iter_"):
            continue
        config_path = idir / "config.json"
        if not config_path.is_file():
            continue

        iter_cfg = json.loads(config_path.read_text())
        curated_path = idir / "curated_ideas.json"
        insights_path = idir / "insights_without_collision.json"
        flags_path = idir / "flags.json"
        fb_path = idir / "feedback.txt"

        curated = json.loads(curated_path.read_text()) if curated_path.is_file() else []
        insights = json.loads(insights_path.read_text()) if insights_path.is_file() else []
        flags = json.loads(flags_path.read_text()) if flags_path.is_file() else {}

        n_loved = sum(1 for v in flags.values() if v in ("loved", "love"))
        n_liked = sum(1 for v in flags.values() if v in ("liked", "like"))
        n_trashed = len(flags) - n_loved - n_liked

        iter_summaries.append({
            "iteration": iter_cfg.get("iteration", "?"),
            "generated": iter_cfg.get("ideas_generated", 0),
            "retained": iter_cfg.get("ideas_retained", 0),
            "curated": len(curated),
            "insights": len(insights),
            "loved": n_loved,
            "liked": n_liked,
            "trashed": n_trashed,
        })

        for c in curated:
            idea_id = c.get("idea_id", "")
            flag = flags.get(idea_id, "trashed")
            entry = {**c, "flag": flag, "iteration": iter_cfg.get("iteration", "?")}
            if flag in ("loved", "love"):
                all_loved.append(entry)
            elif flag in ("liked", "like"):
                all_liked.append(entry)
            else:
                all_trashed.append(entry)

        for c in insights:
            idea_id = c.get("idea_id", "")
            flag = flags.get(idea_id, "unflagged")
            all_insights.append({**c, "flag": flag, "iteration": iter_cfg.get("iteration", "?")})

        if fb_path.is_file():
            fb_text = fb_path.read_text(encoding="utf-8").strip()
            if fb_text:
                all_feedback.append((iter_cfg.get("iteration", "?"), fb_text))

    # Build report
    lines = [f"# {project_path.name} — {brainstorm_dir.name}", ""]
    lines.append(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Iter | Generated | Retained | Curated | Insights | Loved | Liked | Trashed |")
    lines.append("|------|-----------|----------|---------|----------|-------|-------|---------|")
    for s in iter_summaries:
        lines.append(
            f"| {s['iteration']} | {s['generated']} | {s['retained']} | {s['curated']} | "
            f"{s['insights']} | {s['loved']} | {s['liked']} | {s['trashed']} |"
        )
    lines.append("")

    # Feedback
    if all_feedback:
        lines.append("## Feedback")
        lines.append("")
        for iter_num, fb in all_feedback:
            lines.append(f"- **Iter {iter_num}:** {fb}")
        lines.append("")

    # Loved ideas
    if all_loved:
        lines.append("---")
        lines.append("")
        lines.append(f"## Loved Ideas ({len(all_loved)})")
        lines.append("")
        for c in all_loved:
            lines.append(f"### [{c.get('score', '?')}] (iter {c['iteration']})")
            lines.append(f"\n{c.get('text', '')}")
            if c.get("why_selected"):
                lines.append(f"\n**Why selected:** {c['why_selected']}")
            if c.get("source_note"):
                lines.append(f"**Source:** {c['source_note']}")
            if c.get("challenge"):
                lines.append(f"**Challenge:** {c['challenge']}")
            lines.append("")

    # Liked ideas
    if all_liked:
        lines.append("---")
        lines.append("")
        lines.append(f"## Liked Ideas ({len(all_liked)})")
        lines.append("")
        for c in all_liked:
            lines.append(f"### [{c.get('score', '?')}] (iter {c['iteration']})")
            lines.append(f"\n{c.get('text', '')}")
            if c.get("why_selected"):
                lines.append(f"\n**Why selected:** {c['why_selected']}")
            lines.append("")

    # Trashed ideas (shorter format)
    if all_trashed:
        lines.append("---")
        lines.append("")
        lines.append(f"## Trashed Ideas ({len(all_trashed)})")
        lines.append("")
        for c in all_trashed:
            # First line of text only as summary
            first_line = c.get("text", "").split("\n")[0][:150]
            lines.append(f"- [{c.get('score', '?')}] (iter {c['iteration']}) {first_line}")
        lines.append("")

    # Insights without collision (curator pass 2, kept separate from the curated pool)
    if all_insights:
        lines.append("---")
        lines.append("")
        lines.append(f"## Insights Without Collision ({len(all_insights)})")
        lines.append("")
        lines.append("*High-signal observations that did not arise from a true bisociation but are still worth surfacing. Aggregated across all iterations.*")
        lines.append("")
        for c in all_insights:
            flag = c.get("flag", "unflagged")
            flag_label = {"loved": "❤️ LOVED", "liked": "👍 LIKED", "trashed": "🗑️ TRASHED"}.get(flag, "")
            lines.append(f"### [{c.get('score', '?')}] (iter {c['iteration']}) {flag_label}")
            lines.append(f"\n{c.get('text', '')}")
            if c.get("why_kept"):
                lines.append(f"\n**Why kept:** {c['why_kept']}")
            lines.append("")

    report = "\n".join(lines)
    (brainstorm_dir / "REPORT.md").write_text(report, encoding="utf-8")
    return report


def generate_report(project_dir: str, state: dict | None = None) -> str:
    """Backward-compatible wrapper — generates the brainstorm report."""
    return generate_brainstorm_report(project_dir)


# ======================================================================
# PRIVATE HELPERS
# ======================================================================

def _make_fresh_state(brainstorm_id: str) -> dict:
    return {
        "current_iteration": 0,
        "brainstorm_id": brainstorm_id,
        "status": "new",
        "total_ideas_generated": 0,
        "total_loved": 0,
        "total_liked": 0,
        "created_at": datetime.now().isoformat(),
        "last_activity": datetime.now().isoformat(),
    }


def _load_state(project_path: Path) -> dict:
    state_path = project_path / "brainstorm_state.json"
    if state_path.is_file():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return _make_fresh_state("")


def _save_state(project_path: Path, state: dict) -> None:
    state_path = project_path / "brainstorm_state.json"
    _save_json(state_path, state)


def _load_brief(project_path: Path) -> dict:
    with open(project_path / "brief_validated.json", encoding="utf-8") as f:
        return json.load(f)


def _load_text_bank(project_path: Path) -> dict:
    """Load input_bank.yaml. Expects format: text_inputs: {T01: {...}, ...}"""
    with open(project_path / "input_bank.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_domain_history(brainstorm_dir: Path) -> list[dict]:
    path = brainstorm_dir / "domain_history.yaml"
    if path.is_file():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return []


def _load_loved_liked(brainstorm_dir: Path) -> tuple[list[dict], list[dict]]:
    loved, liked = [], []
    loved_path = brainstorm_dir / "loved_ideas.json"
    liked_path = brainstorm_dir / "liked_ideas.json"
    if loved_path.is_file():
        loved = json.loads(loved_path.read_text())
    if liked_path.is_file():
        liked = json.loads(liked_path.read_text())
    return loved, liked


def _load_judge_config(project_path: Path) -> dict:
    path = project_path / "judge_config.json"
    if path.is_file():
        return json.loads(path.read_text())
    return {}


def _update_domain_history(brainstorm_dir: Path, fresh_yaml: str) -> None:
    try:
        bank = yaml.safe_load(fresh_yaml) or {}
        families = []
        for set_id, set_data in bank.get("sets", {}).items():
            families.append({
                "name": set_data.get("name", set_id),
                "set_id": set_id,
                "domains": set_data.get("domains", []),
            })
        history = _load_domain_history(brainstorm_dir)
        history.extend(families)
        path = brainstorm_dir / "domain_history.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(history, f, allow_unicode=True, default_flow_style=False)
    except yaml.YAMLError:
        logger.warning("Could not update domain history")
