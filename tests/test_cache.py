from raiden_viz import cache


def test_get_or_create_produces_and_memoizes(cache_dir):
    calls = []

    def produce(dst):
        calls.append(dst)
        dst.write_bytes(b"hello")

    first = cache.get_or_create("thing.bin", produce)
    second = cache.get_or_create("thing.bin", produce)

    assert first == second
    assert first.read_bytes() == b"hello"
    assert len(calls) == 1, "second call must hit the cache, not re-produce"
