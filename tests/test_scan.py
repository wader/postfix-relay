"""What the daily image scan does with what it finds.

`.github/workflows/scan.yml` is the only thing in the tree that writes to the
issue tracker and the only one that starts another workflow. Both happen on a
clock, against an image this repository published rather than anything a pull
request builds, so no other gate exercises them: the first time a mistake in
that file is noticed is the morning it files the wrong issue -- or the morning
it files one and then never takes it back.

That second case is the one these tests are mostly about. The job used to
dispatch a rebuild and return, which left the remedy automatic and the
verification manual, and nothing in the tree could report back: `ci.yml` has no
`issues` permission, no trigger here fires on another workflow finishing, and
the close step reads the count from the scan its own run already did. So an
issue could only be closed by a *later* run, and the only thing that starts one
is the next day's cron. Issue #381 spent a day open over an image the rebuild
had already fixed. Each assertion below names the step that keeps that from
happening again.

Like `test_ruleset.py` these read files and start nothing, so they are part of
the small half of the suite that needs no docker daemon.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN = REPO_ROOT / ".github" / "workflows" / "scan.yml"


def scan_steps():
    """The one job's steps, keyed by the `name:` each is written under."""
    workflow = yaml.safe_load(SCAN.read_text())
    steps = workflow["jobs"]["trivy"]["steps"]
    return {step["name"]: step for step in steps}


def test_the_rebuild_is_waited_for_rather_than_only_dispatched():
    steps = scan_steps()
    rebuild = steps["Trigger a no-cache rebuild and wait for it"]
    assert rebuild.get("id") == "rebuild"
    assert "gh workflow run ci.yml" in rebuild["run"]
    assert "gh run view" in rebuild["run"], (
        "the step dispatches a rebuild but never looks at the run it started, "
        "so nothing in this workflow can tell whether the remedy worked"
    )
    assert "conclusion=" in rebuild["run"]


def test_a_finished_rebuild_is_re_scanned():
    steps = scan_steps()
    rescan = steps["Re-scan what the rebuild published"]
    assert rescan.get("id") == "rescan"
    assert rescan["if"] == "steps.rebuild.outputs.conclusion == 'success'"
    assert "--ignore-unfixed" in rescan["run"]
    assert "--severity HIGH,CRITICAL" in rescan["run"], (
        "the re-scan has to ask the first scan's question; a wider or narrower "
        "one cannot answer whether the finding that opened the issue is gone"
    )


def test_the_re_scan_compares_digests_before_believing_its_count():
    """ci.yml's "Publish latest" stands down without failing when master's head
    moved under it, so a ci run can conclude success having left `latest`
    exactly where it was. The count then comes back unchanged, and calling
    that a rebuild that did not work would describe a rebuild that never
    reached the tag.
    """
    steps = scan_steps()
    assert "digest=" in steps["Scan the published image"]["run"]
    rescan = steps["Re-scan what the rebuild published"]
    assert rescan["env"]["BEFORE"] == "${{ steps.scan.outputs.digest }}"
    assert "RepoDigests" in rescan["run"]


def test_a_clean_re_scan_closes_the_issue_and_spares_the_run():
    steps = scan_steps()
    close = steps["Close the finding issue"]["if"]
    fail = steps["Fail the run"]["if"]
    assert "steps.rescan.outputs.verdict == 'clean'" in close, (
        "only the first scan can close the issue, so a run that found "
        "something, fixed it and watched the fix land still leaves it open"
    )
    assert "steps.rescan.outputs.verdict != 'clean'" in fail, (
        "the run goes red even when its own rebuild cleared the finding, "
        "which is an email nobody has anything to act on"
    )
    assert "verdict=clean" in steps["Re-scan what the rebuild published"]["run"]


def test_no_condition_reads_a_skipped_step_as_a_number():
    """A step that did not run contributes no outputs, so a reference to one is
    Null -- and github coerces a mismatched comparison to numbers, where Null
    is 0 and so is the string "0", and so is the empty string. Comparing an
    output that may not be there against a numeric literal is therefore true
    exactly when the step did not run, which is the inverse of what such a
    condition is ever written to mean. A word is safe because it parses as NaN
    and NaN equals nothing, itself included.
    """
    workflow = yaml.safe_load(SCAN.read_text())
    steps = workflow["jobs"]["trivy"]["steps"]
    skippable = {s["id"] for s in steps if "if" in s and "id" in s}

    numeric = []
    for step in steps:
        for ref, literal in re.findall(
            r"steps\.(\w+)\.outputs\.\w+\s*[=!]=\s*'([^']*)'", step.get("if", "")
        ):
            if ref not in skippable:
                continue
            try:
                float(literal)
            except ValueError:
                continue
            numeric.append(f"{step['name']}: compares {ref}'s output against '{literal}'")
        if "''" in step.get("if", ""):
            numeric.append(f"{step['name']}: compares against the empty string, which coerces to 0")

    assert not numeric, (
        "these conditions are true exactly when the step they read was "
        "skipped: " + "; ".join(numeric)
    )


def test_every_step_that_reaches_github_says_which_repository():
    """Nothing here is checked out, so `gh` has no git remote to fall back on
    and exits "failed to run git: fatal: not a git repository" before reaching
    the API. `GH_REPO` is what each of those steps has instead.
    """
    missing = [
        name
        for name, step in scan_steps().items()
        if "gh " in step.get("run", "") and "GH_REPO" not in step.get("env", {})
    ]
    assert not missing, f"steps calling gh without GH_REPO set: {missing}"


def test_the_attempt_budget_is_per_finding_and_not_per_issue():
    """The issue is matched on its title alone and held open until the scan is
    clean about everything, so a vulnerability arriving while it is open would
    otherwise inherit the budget the previous one had already spent -- and a
    brand-new one turning up on day three would reach the give-up branch with
    no rebuild ever attempted for it. The ids the attempts were spent on are
    what tell those apart.
    """
    steps = scan_steps()
    assert "ids=" in steps["Scan the published image"]["run"]
    assert "VulnerabilityID" in steps["Scan the published image"]["run"]

    issue = steps["Open or retry the finding issue"]
    assert issue["env"]["IDS"] == "${{ steps.scan.outputs.ids }}"
    assert "seen=" in issue["run"], (
        "the marker records only a count, so nothing says which finding the "
        "attempts were spent on and a new one inherits a spent budget"
    )
    assert "attempts=0" in issue["run"], (
        "nothing restarts the budget, so an id never retried for still lands "
        "in the give-up branch"
    )


def test_a_no_cache_rebuild_is_verified_by_the_whole_suite():
    """A rebuild is a workflow_dispatch of ci.yml, and test.yml is started by a
    push to master, a pull request or a dispatch of its own -- never by that.
    So on a rebuild these two jobs are the only thing that looks at the image
    before `Publish latest` moves the tag onto it, and the package set they
    would be smoke-testing is the one thing about it the suite has never seen.
    """
    ci = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    verify = [job for key, job in ci["jobs"].items() if key.startswith("verify_published")]
    assert len(verify) == 2, f"expected two verify jobs, got {len(verify)}"
    for job in verify:
        named = [s for s in job["steps"] if s["name"].startswith("Run the tests")]
        assert named, (
            f"{job['name']} has no step named 'Run the tests...'; steps are "
            f"{[s['name'] for s in job['steps']]}"
        )
        step = named[0]
        assert step["env"]["NO_CACHE"] == "${{ inputs.no-cache || false }}"
        assert "pytest -m smoke" in step["run"], "a merge must still only smoke-test"
        body = step["run"].replace("pytest -m smoke", "")
        assert "pytest" in body, (
            f"{job['name']} smoke-tests a no-cache rebuild, which is the one "
            f"image no run of the whole suite ever sees"
        )


def test_a_recorded_attempt_is_read_back_before_it_is_spent():
    """The attempt counter is the only state this job keeps, and it keeps it in
    an issue body other things write to -- #381 acquired an assignee nothing in
    the workflow sets, 87 seconds after the workflow created it. A marker that
    goes missing reads back as attempt 0, so the budget restarts and the cap
    never engages: the same silent shape as the sed no-op of #379, which cost a
    day of retries before anyone noticed it was not counting.
    """
    issue = scan_steps()["Open or retry the finding issue"]["run"]
    assert "confirmStamp" in issue, (
        "the marker is written and never read back, so losing it is silent"
    )
    assert issue.count("confirmStamp \"$next\"") == 2, (
        "both branches that bump the counter must confirm it landed"
    )
    after = issue.split("confirmStamp()", 1)[1]
    assert "exit 1" in after, (
        "a run that cannot record an attempt must not go on to spend one"
    )
