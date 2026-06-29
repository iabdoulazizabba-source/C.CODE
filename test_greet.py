from greet import greet


def test_greet():
    assert greet("world") == "Hello, world!"


def test_greet_empty_name():
    assert greet("") == "Hello, !"
