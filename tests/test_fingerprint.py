from rove.fingerprint import skeleton_hash


def test_same_skeleton_same_hash():
    assert skeleton_hash("A|B|C") == skeleton_hash("A|B|C")


def test_different_skeleton_different_hash():
    assert skeleton_hash("A|B") != skeleton_hash("A|B|C")


def test_hash_is_hex_sha256():
    h = skeleton_hash("x")
    assert len(h) == 64 and int(h, 16) >= 0
