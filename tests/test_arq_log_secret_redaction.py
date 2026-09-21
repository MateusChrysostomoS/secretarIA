"""arq's NATIVE job log must not carry the OTP, the reset link or the e-mail.

`arq.worker.Worker.run_job` logs every job as `→ job_ref(repr(args)…)` through
stdlib `logging` — structlog's `redact_secrets` never sees it. For
`send_transactional_email('patient_access_otp', <address>, {'code': …})` that line
printed the portal OTP in clear text (docs/CHECKPOINT_secretaria_arq_log_secret_leak.md).

These tests prove ABSENCE, and do so non-vacuously: each one first asserts that
the job line WAS logged (job name + template visible), then that the synthetic
secret is in none of the captured lines.
"""

import inspect
import logging
from unittest.mock import AsyncMock, patch

import pytest
from arq.utils import args_to_string
from arq.worker import Worker, logger as arq_worker_logger

import secretaria.core.logging as core_logging
from secretaria.core.logging import (
    ArqJobArgsRedactionFilter,
    install_arq_log_redaction,
    redact_free_text,
)
from secretaria.workers import tasks

# Synthetic, distinctive values — never anything from production.
_OTP = "918273"
_RESET_TOKEN = "rst-7c1e5a9b2f4d8e6a"
_RESET_LINK = f"https://portal.example.test/redefinir-senha?token={_RESET_TOKEN}"
_ADDRESS = "paciente.sintetico@example.test"


def _emit_arq_job_start_line(job: str, args: tuple, kwargs: dict | None = None) -> None:
    """Emit EXACTLY the line arq's `Worker.run_job` emits, with arq's own
    logger and arq's own `args_to_string` (truncation included)."""
    s = args_to_string(args, kwargs or {})
    arq_worker_logger.info("%6.2fs → %s(%s)%s", 0.01, f"abc123:{job}", s, "")


def test_arq_run_job_still_logs_args_the_way_this_filter_expects():
    """Drift guard: if a future arq changes how it logs job args, fail here so
    the filter is re-checked instead of silently no longer matching."""
    source = inspect.getsource(Worker.run_job)
    assert "args_to_string(args, kwargs)" in source
    assert "'%6.2fs → %s(%s)%s'" in source
    assert arq_worker_logger.name == "arq.worker"


@pytest.fixture
def arq_redaction():
    install_arq_log_redaction()
    # Left installed afterwards — that is the state setup_logging() leaves too.


class _RecordingLogger:
    """Stand-in for the job module's structlog logger. structlog caches loggers
    on first use (bound to whatever stdout existed then), so neither capsys nor
    `capture_logs` reliably sees them — record every call directly instead."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def bind(self, **_kwargs):
        return self

    def __getattr__(self, _level: str):
        def emit(event, *args, **kwargs):
            self._sink.append(repr((event, args, kwargs)))

        return emit


async def _run_real_job_capturing(
    caplog, capsys, monkeypatch, template: str, variables: dict
) -> str:
    caplog.set_level(logging.DEBUG)
    structlog_events: list[str] = []
    monkeypatch.setattr(tasks, "logger", _RecordingLogger(structlog_events))
    with patch(
        "secretaria.workers.tasks.send_transactional_email_message",
        new=AsyncMock(return_value=True),
    ):
        _emit_arq_job_start_line("send_transactional_email", (template, _ADDRESS, variables))
        await tasks.send_transactional_email({}, template, _ADDRESS, variables)
    captured = capsys.readouterr()
    # Everything that reached ANY sink: stdlib records (arq), the job's own
    # structlog events, and raw stdout/stderr.
    return "\n".join(
        [r.getMessage() for r in caplog.records] + structlog_events + [captured.out, captured.err]
    )


@pytest.mark.asyncio
async def test_otp_code_never_reaches_the_arq_job_log(arq_redaction, caplog, capsys, monkeypatch):
    output = await _run_real_job_capturing(
        caplog, capsys, monkeypatch, "patient_access_otp", {"code": _OTP, "ttl_minutes": 10}
    )

    # Non-vacuous: the job line WAS logged, with its harmless parts intact.
    assert "send_transactional_email(" in output
    assert "patient_access_otp" in output
    assert "worker_transactional_email_processed" in output
    # And the secrets are nowhere.
    assert _OTP not in output
    assert _ADDRESS not in output
    assert "***REDACTED***" in output


@pytest.mark.asyncio
async def test_password_reset_link_never_reaches_the_arq_job_log(
    arq_redaction, caplog, capsys, monkeypatch
):
    output = await _run_real_job_capturing(
        caplog,
        capsys,
        monkeypatch,
        "password_reset",
        {"name": "Fulana", "link": _RESET_LINK, "ttl_minutes": 60},
    )

    assert "send_transactional_email(" in output
    assert "password_reset" in output
    assert "worker_transactional_email_processed" in output
    assert _RESET_TOKEN not in output
    assert "redefinir-senha" not in output
    assert _ADDRESS not in output


def test_a_value_cut_by_arqs_80_char_truncation_is_still_redacted(arq_redaction, caplog):
    """arq truncates the args string at 80 chars — a long address pushes the OTP
    across the cut, leaving `'code': '9182…` with no closing quote."""
    caplog.set_level(logging.INFO)
    long_address = "synthetic.patient.x@clinica.example.test"  # 40 chars: cut hits the code
    _emit_arq_job_start_line(
        "send_transactional_email",
        ("patient_access_otp", long_address, {"code": _OTP, "ttl_minutes": 10}),
    )
    line = caplog.records[-1].getMessage()

    assert "send_transactional_email(" in line
    raw = args_to_string(
        ("patient_access_otp", long_address, {"code": _OTP, "ttl_minutes": 10}), {}
    )
    assert raw.endswith("…") and "'code': '9" in raw  # the cut really lands in the code
    for prefix_len in range(2, len(_OTP) + 1):
        assert f"'{_OTP[:prefix_len]}" not in line
    assert "clinica.example" not in line


def test_filter_is_generic_across_jobs_and_kwargs(arq_redaction, caplog):
    caplog.set_level(logging.INFO)
    _emit_arq_job_start_line("some_future_job", ("tenant-1",), {"access_token": "tok-synth-42"})
    line = caplog.records[-1].getMessage()

    assert "some_future_job(" in line
    assert "tenant-1" in line  # harmless positional args survive
    assert "tok-synth-42" not in line


def test_bytes_secret_values_are_redacted(arq_redaction, caplog):
    """repr(bytes) is `b'…'` — the prefix must not stop the quoted value from matching."""
    caplog.set_level(logging.INFO)
    _emit_arq_job_start_line("some_job", ({"code": b"918273"},), {"link": b"rst-bytes-77"})
    line = caplog.records[-1].getMessage()

    assert "some_job(" in line
    assert "918273" not in line
    assert "rst-bytes-77" not in line


def test_harmless_job_lines_are_left_untouched():
    text = "0.01s → abc:transcribe_audio_message('msg-1', 'tenant-2', {'kind': 'audio'})"
    assert redact_free_text(text) == text


def test_setup_logging_installs_the_filter_on_arq_loggers(monkeypatch):
    for name in ("arq.worker", "arq.jobs"):
        target = logging.getLogger(name)
        for f in [f for f in target.filters if isinstance(f, ArqJobArgsRedactionFilter)]:
            target.removeFilter(f)
    monkeypatch.setattr(core_logging, "_configured", False)

    core_logging.setup_logging()
    core_logging.install_arq_log_redaction()  # idempotent: still exactly one

    for name in ("arq.worker", "arq.jobs"):
        filters = logging.getLogger(name).filters
        assert sum(isinstance(f, ArqJobArgsRedactionFilter) for f in filters) == 1
