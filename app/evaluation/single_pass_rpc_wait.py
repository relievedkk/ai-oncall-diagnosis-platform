"""Compare paired RPC overhead across routes sharing an endpoint."""

from itertools import combinations


def _comparable(f):
    before, after = f.data["before_pairs"], f.data["after_pairs"]
    return (
        min(before, after) >= 12
        and min(len(f.data["before_buckets"]), len(f.data["after_buckets"])) >= 3
        and 0.65 <= after / before <= 1.5
    )


def _stable_handler(f):
    before, after = f.data["before_handler_p90_us"], f.data["after_handler_p90_us"]
    return before > 0 and 0.67 * before <= after <= 1.5 * before


def _wait_shift(f):
    before, after = f.data["before_wait_us"], f.data["after_wait_us"]
    deltas = [post - pre for pre, post in zip(before, after, strict=True)]
    if min(deltas) > 2 * max(1000, before[2]) and max(deltas) <= 1.5 * min(deltas):
        return deltas[1]
    return None


def _stable_wait(f):
    return _stable_handler(f) and all(
        abs(post - pre) <= max(1000, 0.5 * pre)
        for pre, post in zip(f.data["before_wait_us"], f.data["after_wait_us"], strict=True)
    )


def _similar(*shifts):
    return max(shifts) <= 1.5 * min(shifts)


def _same_window(facts):
    for period in ("before", "after"):
        coverage = [set(f.data[period + "_buckets"]) for f in facts]
        shared = len(set.intersection(*coverage))
        if shared < 3 or any(shared < 0.8 * len(buckets) for buckets in coverage):
            return False
    return True


def endpoint_wait_contrast(evidence, service):
    """Require a shared endpoint, distinct peer services, and an unaffected route."""
    incoming, outgoing, controls = [], [], {}
    for f in evidence.facts:
        if f.source != "traces" or f.family != "rpc_wait" or not _comparable(f):
            continue
        callee = f.data["callee"]
        shift = _wait_shift(f)
        if callee == service and f.service != service and shift is not None:
            incoming.append((f, shift))
        elif (
            f.service == service and callee != service and shift is not None and _stable_handler(f)
        ):
            outgoing.append((f, shift))
        elif f.service != service and callee != service and _stable_wait(f):
            controls.setdefault(f.service, []).append(f)

    # Compare distinct callers with their observed unaffected routes.
    anchored = [(f, shift) for f, shift in incoming if f.service in controls]
    incoming_stable = [(f, shift) for f, shift in anchored if _stable_handler(f)]
    for (a, shift_a), (b, shift_b) in combinations(incoming_stable, 2):
        if a.service != b.service and _similar(shift_a, shift_b):
            for control_a in controls[a.service]:
                for control_b in controls[b.service]:
                    refs = (a, b, control_a, control_b)
                    if _same_window(refs):
                        return refs

    # One affected incoming route and two outgoing routes share the endpoint.
    for entry, incoming_shift in anchored:
        for (a, shift_a), (b, shift_b) in combinations(outgoing, 2):
            if a.data["callee"] != b.data["callee"] and _similar(incoming_shift, shift_a, shift_b):
                for control in controls[entry.service]:
                    refs = (entry, a, b, control)
                    if _same_window(refs):
                        return refs
    return ()
