from homehub.pc_agent.auth import NONCE_HEADER, SIGNATURE_HEADER, TIMESTAMP_HEADER, ReplayGuard, new_key, sign, signed_headers, verify


def test_signatures_cover_method_path_time_nonce_and_body():
    key = new_key()
    assert len(key) == 64 and key != new_key()
    body = b'{"what": "summary"}'
    headers = signed_headers(key, "POST", "/op/diagnostics.summary", body, now=1_792_800_000)
    assert headers[TIMESTAMP_HEADER] == "1792800000" and len(headers[NONCE_HEADER]) == 16
    assert headers[SIGNATURE_HEADER] == sign(key, "POST", "/op/diagnostics.summary", "1792800000", headers[NONCE_HEADER], body)
    guard = ReplayGuard()
    assert verify(key, headers, "POST", "/op/diagnostics.summary", body, guard, now=1_792_800_010) == (True, "")
    assert verify(key, headers, "POST", "/op/diagnostics.summary", body, guard, now=1_792_800_011) == (False, "replayed request")
    fresh = signed_headers(key, "POST", "/op/diagnostics.summary", body, now=1_792_800_000)
    assert verify(key, fresh, "POST", "/op/fix.reboot", body, ReplayGuard(), now=1_792_800_000)[1] == "bad signature"
    assert verify(key, fresh, "POST", "/op/diagnostics.summary", b"{}", ReplayGuard(), now=1_792_800_000)[1] == "bad signature"
    assert verify(new_key(), fresh, "POST", "/op/diagnostics.summary", body, ReplayGuard(), now=1_792_800_000)[1] == "bad signature"
    assert verify(key, fresh, "POST", "/op/diagnostics.summary", body, ReplayGuard(), now=1_792_800_100)[1] == "request too old or clock skew"
    assert verify(key, {}, "POST", "/op/x", b"", ReplayGuard())[1] == "missing signature headers"
    assert verify(key, {**fresh, TIMESTAMP_HEADER: "soon"}, "POST", "/op/diagnostics.summary", body, ReplayGuard())[1] == "bad timestamp"
