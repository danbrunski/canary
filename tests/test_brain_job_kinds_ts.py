"""One brain job-kind vocabulary (E03.6): the dashboard's type lists exactly
the kinds ``POST /control/brain/jobs`` accepts, enforced rather than
remembered. Two kinds were added to the backend while the type still listed
the original two."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from src.control.brain_jobs import JOB_KINDS, JOB_STATES, JobKind, JobState

BRAIN_TYPES = Path("src/dashboard/static/src/types/brain.ts")


def _ts_job_kinds() -> list[str]:
    block = BRAIN_TYPES.read_text().split("export const BRAIN_JOB_KINDS = [", 1)[1]
    return re.findall(r'"([a-z_]+)"', block.split("] as const", 1)[0])


def _ts_job_states() -> list[str]:
    line = BRAIN_TYPES.read_text().split("export type BrainJobState =", 1)[1].split(";", 1)[0]
    return re.findall(r'"([a-z_]+)"', line)


def test_the_dashboard_lists_every_backend_job_kind_in_order() -> None:
    assert _ts_job_kinds() == list(JOB_KINDS)


def test_the_backend_literal_and_tuple_agree() -> None:
    assert list(get_args(JobKind)) == list(JOB_KINDS)
    assert list(get_args(JobState)) == list(JOB_STATES)


def test_the_dashboard_lists_every_backend_job_state() -> None:
    assert _ts_job_states() == list(JOB_STATES)


def test_the_job_type_uses_the_shared_kind_list() -> None:
    text = BRAIN_TYPES.read_text()
    interface = text.split("export interface BrainJob {", 1)[1].split("}", 1)[0]
    assert "kind: BrainJobKind;" in interface
