import unittest
from combo_posting.main import hello


class TestHello(unittest.TestCase):
    def test_hello_default(self):
        self.assertEqual(hello(), "Hello, World!")

    def test_hello_name(self):
        self.assertEqual(hello("Alice"), "Hello, Alice!")


if __name__ == "__main__":
    unittest.main()