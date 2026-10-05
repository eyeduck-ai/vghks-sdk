"""Bounded real auth observations owned by the test application, not the SDK."""

from __future__ import annotations

import secrets
import string
from pathlib import Path

from ..contracts.auth_evidence import expected_anonymous_challenge as expected_anonymous_challenge
from ..core.config import PortalCredentials
from ..core.errors import ErrorInfo, NotAuthenticatedError, error_info
from ..core.operations import operation_spec
from ..core.transport import _AnonymousAuth
from ..local_io import write_json_atomic
from ..models import to_jsonable
from ..sdk import VghksSDK
from .login import negative_login, password_post_budget
from .profile import LiveTestStep, _run_step


def run_before_login_checks(config, steps, *, credentials, settings, root: Path, **capture) -> bool:
    """Return whether a correct login can follow the intentional rejection.

    An independent anonymous Session performs one catalog query with a zero
    password budget. The negative Session can send one password POST only.
    Unknown negative outcomes stop later credential submissions in this run.
    """
    for name in ("unauthenticated", "negative_before_login"):
        if (
            credentials is None
            or settings is None
            or (name == "negative_before_login" and config.login_negative_attempts == 0)
        ):
            steps.append(
                LiveTestStep(
                    name="failures.live." + name,
                    status="NO_SAMPLE",
                    issue=ErrorInfo(
                        "TEST_CREDENTIALS_UNAVAILABLE"
                        if credentials is None
                        else "NEGATIVE_TEST_DISABLED",
                        "DATA",
                    ),
                    details={"attempted": False, "evidence": "LIVE"},
                )
            )
            continue

        def anonymous():
            # No real credentials are attached to this SDK and no ensure/login
            # is called. A known challenge is observed before any recovery.
            with VghksSDK(
                settings=settings,
                credentials=PortalCredentials("ANONYMOUS-PROBE", "unused"),
                **capture,
            ) as probe:
                spec = operation_spec("prq.upload_types")
                with password_post_budget(probe._runtime.transport.session, limit=0) as counts:
                    try:
                        probe._runtime.request_json(
                            spec,
                            settings.prq_base_url.rstrip("/") + "/QueryUploadMR.do",
                            data=dict(spec.operation_values),
                            auth=_AnonymousAuth(),
                        )
                    except NotAuthenticatedError as exc:
                        return {
                            "evidence": "LIVE_UNAUTHENTICATED",
                            "prior_login": False,
                            "expected_challenge": True,
                            "observed_code": exc.info.code,
                            "error": to_jsonable(error_info(exc)),
                            **counts,
                        }
                    return {
                        "evidence": "LIVE_UNAUTHENTICATED",
                        "prior_login": False,
                        "expected_challenge": False,
                        "observed_code": "",
                        **counts,
                    }

        def reject_once():
            alphabet = string.ascii_letters + string.digits
            wrong = "".join(secrets.choice(alphabet) for _ in range(12))
            while wrong == credentials.password:
                wrong = "".join(secrets.choice(alphabet) for _ in range(12))
            with VghksSDK(
                settings=settings,
                credentials=PortalCredentials(credentials.username, wrong),
                **capture,
            ) as negative:
                try:
                    value = negative_login(
                        negative,
                        lazy=False,
                        audit_path=root / "parsed/failures/negative-post-counts.json",
                    )
                    return {**value, "position": "BEFORE_CORRECT_LOGIN"}
                finally:
                    write_json_atomic(
                        root / "parsed/failures/negative-password-status.json",
                        negative.auth.password_status,
                    )
                    write_json_atomic(
                        root / "parsed/failures/negative-connections.json",
                        negative.connection_status(),
                    )

        _, error = _run_step(
            steps,
            name="failures.live." + name,
            operation=anonymous if name == "unauthenticated" else reject_once,
            operation_key="prq.upload_types" if name == "unauthenticated" else "portal.login",
            output_path=root / "parsed/failures/live" / (name + ".json"),
            root=root,
            summarize=lambda value: value,
            classify=(lambda value: "OK" if value["expected_challenge"] else "NO_SAMPLE")
            if name == "unauthenticated"
            else None,
            **capture,
        )
        if name == "negative_before_login" and error is not None:
            return False
    return True
