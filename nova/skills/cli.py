"""`nova skills list | show <id> | delete <id>`: what Nova has learned (the JSON files in data/skills/)."""

from __future__ import annotations

import json

from .. import paths
from .book import Book


def main(action: str = "list", skill_id: str | None = None) -> int:
    book = Book(paths.SKILLS)
    if action == "list":
        skills = book.all()
        if not skills:
            print("Nothing learned yet. Ask Nova something new, or say: when I say study mode, open Canvas and Notion")
            return 0
        for skill in skills:
            mark = "stale" if skill.stale else ("taught" if skill.taught else "learned")
            print(f"{skill.id:<40} {mark:<8} used {skill.uses:>3}x  {skill.name}")
        print(f"\n{len(skills)} skill(s) in {paths.SKILLS}")
        return 0
    skill = book.get(skill_id or "")
    if skill is None:
        print(f"No skill called {skill_id!r}. `nova skills list` shows the ids.")
        return 1
    if action == "show":
        print(json.dumps(skill.to_json(), indent=2, ensure_ascii=False))
        return 0
    book.delete(skill.id)
    print(f"Forgot {skill.name!r}")
    return 0
