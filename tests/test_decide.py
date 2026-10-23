from app.decide import decide, limit_gain


def d(**kw):
    base = dict(media="audio", ext="mp3", family="mp3", measured=-21.0, true_peak=-9.0, target=-16.0,
                ceiling=-1.0, threshold=6.0, override=False, strategy="gain", tools={"mp3gain"})
    base.update(kw)
    return decide(**base)


def test_small_change_is_ok():
    assert d(measured=-16.3).action == "ok"


def test_hysteresis_bandwidth_skips_changes_within_band():
    # target is -16.0; at measured -18.0 (+2.0 dB needed), hysteresis of 2.0 keeps it as 'ok'
    assert d(measured=-18.0, hysteresis=2.0).action == "ok"
    assert d(measured=-14.0, hysteresis=2.0).action == "ok"
    # outside the +/-2.0 dB hysteresis bandwidth, the change is applied
    assert d(measured=-18.5, hysteresis=2.0).action == "apply"


def test_gain_tool_used_for_mp3_when_installed():
    x = d()
    assert (x.action, x.method, x.gain) == ("apply", "mp3gain", 5.0)


def test_large_change_is_parked_with_proposal():
    x = d(measured=-30.0, true_peak=-20.0)           # +14 dB wanted
    assert (x.action, x.reason, x.gain) == ("park", "large_change", 14.0)


def test_override_applies_large_change():
    x = d(measured=-30.0, true_peak=-20.0, override=True)
    assert x.action == "apply" and x.method == "mp3gain"


def test_true_peak_limits_positive_gain():
    gain, capped = limit_gain(7.0, true_peak=-3.0, ceiling=-1.0)
    assert (gain, capped) == (2.0, True)


def test_negative_gain_is_not_limited_by_peak():
    assert limit_gain(-5.0, true_peak=-0.1, ceiling=-1.0) == (-5.0, False)


def test_lossless_uses_volume():
    x = d(ext="flac", family="lossless", tools=set())
    assert (x.action, x.method) == ("apply", "volume")


def test_lossy_without_tool_is_parked_for_user_choice():
    x = d(ext="ogg", family="vorbis", tools={"mp3gain"})
    assert (x.action, x.method, x.reason) == ("park", "reencode", "lossy_no_gain_tool")


def test_aac_needs_aacgain_in_mp4_container():
    raw_aac = d(ext="aac", family="aac", tools={"aacgain"})
    m4a = d(ext="m4a", family="aac", tools={"aacgain"})
    assert raw_aac.reason == "lossy_no_gain_tool"
    assert (m4a.action, m4a.method) == ("apply", "aacgain")


def test_skip_strategy_skips_all_lossy():
    x = d(strategy="skip")
    assert (x.action, x.reason) == ("skip", "lossy_skipped")


def test_reencode_strategy_reencodes_lossy_without_tool():
    x = d(ext="ogg", family="vorbis", strategy="reencode", tools=set())
    assert (x.action, x.method) == ("apply", "reencode")


def test_video_lossy_audio_is_reencoded_at_same_bitrate():
    x = d(media="video", ext="mp4", family="aac", strategy="gain", tools=set())
    assert (x.action, x.method) == ("apply", "reencode")


def test_unsupported_codec_is_error():
    assert d(family=None).action == "error"
