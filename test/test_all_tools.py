#!/usr/bin/env python3
"""Test all 30 MCP tools against a live Agda process."""

import asyncio
import sys
import os
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agda_mcp.server import agda, agda_load, agda_hover, agda_definition, \
    agda_infer, agda_compute, agda_case_split, agda_goal_info, agda_auto, \
    agda_why_in_scope, agda_give, agda_elaborate_give, agda_refine, \
    agda_intro, agda_refine_or_intro, agda_goal_type, agda_context, \
    agda_goal_type_context_infer, agda_goal_type_context_check, \
    agda_infer_in_goal, agda_compute_in_goal, agda_helper_function, \
    agda_why_in_scope_goal, agda_module_contents_goal, agda_solve_one, \
    agda_solve_all, agda_auto_all, agda_constraints, agda_metas, \
    agda_search_about, agda_module_contents

TEMPLATE = Path(__file__).parent / "Test.agda"
WORK = Path(__file__).parent / "_TestWork.agda"
MODULE_LINE = "module _TestWork where\n"

passed = 0
failed = 0


def result_ok(result: str) -> bool:
    """Check that a result is not an error and not empty."""
    if not result:
        return False
    if result.startswith("Error:") and "no result" in result.lower():
        return False
    return True


async def test(name: str, coro, *, expect_error=False):
    global passed, failed
    try:
        result = await coro
        result_str = str(result).replace('\n', '\n         ')
        is_error = isinstance(result, str) and result.startswith("Error:")
        if expect_error:
            ok = is_error or result_ok(result)
        else:
            ok = result_ok(result) and not is_error
        status = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  [{status}] {name}")
        print(f"         {result_str[:300]}")
    except Exception as e:
        failed += 1
        print(f"  [FAIL] {name}")
        print(f"         Exception: {e}")
    print()


def reset_file():
    """Reset working file to template with matching module name."""
    content = TEMPLATE.read_text()
    content = content.replace("module Test where", "module _TestWork where", 1)
    WORK.write_text(content)
    agda._loaded_files.discard(str(WORK.resolve()))


async def main():
    global passed, failed
    fp = str(WORK.resolve())

    # =========================================================================
    print("=" * 60)
    print("PHASE 1: Read-only tools (original 9 + new inspection tools)")
    print("=" * 60)
    reset_file()

    # 1. agda_load
    await test("agda_load", agda_load(fp))

    # 2. agda_hover (on 'add' at line 6)
    await test("agda_hover", agda_hover(fp, 6, 1))

    # 3. agda_definition (on 'Nat' at line 5)
    await test("agda_definition", agda_definition(fp, 5, 7))

    # 4. agda_infer
    await test("agda_infer", agda_infer(fp, "add"))

    # 5. agda_compute
    await test("agda_compute", agda_compute(fp, "add 2 3"))

    # 6. agda_goal_info (goal 0)
    await test("agda_goal_info", agda_goal_info(fp, 0))

    # 7. agda_why_in_scope
    await test("agda_why_in_scope", agda_why_in_scope(fp, "Nat"))

    # 8. agda_goal_type (goal 0)
    await test("agda_goal_type", agda_goal_type(fp, 0))

    # 9. agda_context (goal 0)
    await test("agda_context", agda_context(fp, 0))

    # 10. agda_goal_type_context_infer (goal 0, expr "n")
    await test("agda_goal_type_context_infer", agda_goal_type_context_infer(fp, 0, "n"))

    # 11. agda_goal_type_context_check (goal 0, expr "add n n")
    await test("agda_goal_type_context_check", agda_goal_type_context_check(fp, 0, "add n n"))

    # 12. agda_infer_in_goal (goal 0, expr "n")
    await test("agda_infer_in_goal", agda_infer_in_goal(fp, 0, "n"))

    # 13. agda_compute_in_goal (goal 0, expr "add 1 1")
    await test("agda_compute_in_goal", agda_compute_in_goal(fp, 0, "add 1 1"))

    # 14. agda_helper_function (goal 2 = myLemma, with partial application)
    await test("agda_helper_function", agda_helper_function(fp, 2, "h x y"))

    # 15. agda_why_in_scope_goal (goal 0, name "n")
    await test("agda_why_in_scope_goal", agda_why_in_scope_goal(fp, 0, "n"))

    # 16. agda_module_contents_goal (goal 0, module "Agda.Builtin.Nat")
    await test("agda_module_contents_goal", agda_module_contents_goal(fp, 0, "Agda.Builtin.Nat"))

    # 17. agda_constraints
    await test("agda_constraints", agda_constraints(fp))

    # 18. agda_metas
    await test("agda_metas", agda_metas(fp))

    # 19. agda_search_about
    await test("agda_search_about", agda_search_about(fp, "Nat"))

    # 20. agda_module_contents
    await test("agda_module_contents", agda_module_contents(fp, "Agda.Builtin.Nat"))

    # =========================================================================
    print("=" * 60)
    print("PHASE 2: Solve/auto tools")
    print("=" * 60)

    # 21. agda_solve_all (likely no solutions yet, but should not crash)
    reset_file()
    await test("agda_solve_all", agda_solve_all(fp))

    # 22. agda_solve_one (goal 0, likely no solution)
    reset_file()
    await test("agda_solve_one", agda_solve_one(fp, 0))

    # 23. agda_auto (goal 1 = id')
    reset_file()
    await test("agda_auto (goal 1: id')", agda_auto(fp, 1))

    # 24. agda_auto_all
    reset_file()
    await test("agda_auto_all", agda_auto_all(fp))

    # =========================================================================
    print("=" * 60)
    print("PHASE 3: Goal manipulation tools (modify source)")
    print("=" * 60)

    # 25. agda_case_split (goal 0, var "n" in double)
    reset_file()
    await test("agda_case_split", agda_case_split(fp, 0, "n"))

    # 26. agda_refine (goal 1: id', expr "x")
    reset_file()
    await test("agda_refine", agda_refine(fp, 1, "x"))

    # 27. agda_intro (goal 2: myLemma)
    reset_file()
    await test("agda_intro", agda_intro(fp, 2), expect_error=True)

    # 28. agda_refine_or_intro (goal 1: id', expr "x")
    reset_file()
    await test("agda_refine_or_intro", agda_refine_or_intro(fp, 1, "x"))

    # 29. agda_give (goal 0: double, expr "add n n")
    reset_file()
    await test("agda_give", agda_give(fp, 0, "add n n"))

    # 30. agda_elaborate_give (goal 1: id', expr "x")
    reset_file()
    await test("agda_elaborate_give", agda_elaborate_give(fp, 1, "x"))

    # =========================================================================
    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
    print("=" * 60)

    # Cleanup
    WORK.unlink(missing_ok=True)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
