"""The checks that gate a merge, against the jobs that report them.

`.github/rulesets/master.json` records the required status checks so the gate
can be checked rather than believed. These tests are what keep that record
true: a required check is matched by a job's display `name:`, so renaming a
job without editing the ruleset leaves a context that never reports, and a
context that never reports blocks every pull request until someone with admin
rights notices.

Unlike every other module here these tests read files and start nothing, so
they are the one part of the suite that needs no docker daemon.
"""

import json
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RULESET = REPO_ROOT / ".github" / "rulesets" / "master.json"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def ruleset():
    return json.loads(RULESET.read_text())


def required_contexts():
    rules = [r for r in ruleset()["rules"] if r["type"] == "required_status_checks"]
    assert len(rules) == 1, f"expected one required_status_checks rule, got {len(rules)}"
    return [c["context"] for c in rules[0]["parameters"]["required_status_checks"]]


def job_display_names():
    """Every check name a workflow can report, mapped to the file reporting it.

    A job without a `name:` reports under its key -- that is how the
    auto-merge job appears -- so the key is the default rather than a skip.
    """
    names = {}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for key, job in yaml.safe_load(workflow.read_text())["jobs"].items():
            names[job.get("name", key)] = workflow.name
    return names


def test_every_required_check_names_a_job_that_exists():
    reported = job_display_names()
    missing = [c for c in required_contexts() if c not in reported]
    assert not missing, (
        f"required contexts naming no job: {missing}. Such a context never "
        f"reports and blocks every pull request. Names that do report: "
        f"{sorted(reported)}"
    )


def test_the_ruleset_still_gates_the_default_branch():
    """A re-export made after fiddling in the web UI can bring back a file
    that records a gate which does not gate."""
    recorded = ruleset()
    assert recorded["enforcement"] == "active"
    assert recorded["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert recorded["bypass_actors"] == []
