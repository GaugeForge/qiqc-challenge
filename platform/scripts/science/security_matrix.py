"""Pure classification: blocked observations are not invented positive controls."""


def reachable(result):
    return (
        result.get("status") is not None or result.get("application_response") is True
    )


def classify(before, after, expectation):
    if expectation == "dns_unknown":
        return "inconclusive_authoritative_dns_sink_not_tested"
    if expectation.startswith("http_"):
        return (
            "passed"
            if after.get("status") == int(expectation.removeprefix("http_"))
            else "failed"
        )
    if expectation == "deny":
        if reachable(after):
            return "failed_reachable_after_restriction"
        return (
            "passed_with_positive_control"
            if reachable(before)
            else "blocked_observed_positive_control_unavailable"
        )
    return "invalid_expectation"
