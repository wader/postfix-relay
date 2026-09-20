"""What the build workflow runs against the image it has just published.

`.github/workflows/ci.yml` is what puts an image in front of users, and the
two **Verify Published Image** jobs are the last thing to look at one before
**Publish latest** moves the tag onto it. What they run is not the same
question on every run, and the difference is the point of this module: a merge
has `test.yml` running the whole suite against a build of the same tree in
parallel, so the smoke tests are all that is left to learn, while a no-cache
rebuild is a `workflow_dispatch` of `ci.yml` alone and `test.yml` never sees
it at all.

Like `test_ruleset.py` these read a file and start nothing, so they are part of
the small half of the suite that needs no docker daemon.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def verify_jobs():
    """The two jobs that pull the published image back and test it."""
    workflow = yaml.safe_load(CI.read_text())
    return [job for key, job in workflow["jobs"].items() if key.startswith("verify_published")]


def test_a_no_cache_rebuild_is_verified_by_the_whole_suite():
    """A rebuild is a `workflow_dispatch` of this workflow, and `test.yml` is
    started by a push to `master`, a pull request or a dispatch of its own --
    never by that one. So on a rebuild these two jobs are the only thing that
    looks at the image before the tag moves onto it, and the package set they
    would be smoke-testing is the one thing about that image the suite has
    never seen.
    """
    jobs = verify_jobs()
    assert len(jobs) == 2, f"expected two verify jobs, got {len(jobs)}"
    for job in jobs:
        named = [s for s in job["steps"] if s["name"].startswith("Run the tests")]
        assert named, (
            f"{job['name']} has no step named 'Run the tests...'; steps are "
            f"{[s['name'] for s in job['steps']]}"
        )
        step = named[0]
        assert step["env"]["NO_CACHE"] == "${{ inputs.no-cache || false }}"
        assert "pytest -m smoke" in step["run"], "a merge must still only smoke-test"
        assert "pytest" in step["run"].replace("pytest -m smoke", ""), (
            f"{job['name']} smoke-tests a no-cache rebuild, which is the one "
            f"image no run of the whole suite ever sees"
        )
