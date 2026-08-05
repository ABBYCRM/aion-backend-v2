"""GitHub scenario matcher — thin wrapper over scenarios.match_scenarios.

The 5-pack unified matcher lives in app/skills/clients/scenarios.py.
This module keeps the existing public surface (github_scenario_match,
github_scenario_index) and the existing route contract for
github.scenario.match / github.scenario.index, but delegates the
actual row lookup to the shared loader so adding a new pack is
a one-line change in scenarios.PACKS.
"""
from __future__ import annotations

from typing import Any

from .scenarios import match_scenarios, scenario_index


async def github_scenario_match(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return match_scenarios(
        trigger=args.get("trigger", ""),
        pack="github",
        condition=args.get("condition"),
        category=args.get("category"),
        context=args.get("context"),
        limit=int(args.get("limit") or 5),
    )


async def github_scenario_index(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return await scenario_index({"pack": "github"}, ctx)
