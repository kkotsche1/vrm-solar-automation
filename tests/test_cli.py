from __future__ import annotations

import unittest

from vrm_solar_automation.cli import build_parser


class CliTests(unittest.TestCase):
    def test_build_parser_accepts_control_command(self) -> None:
        args = build_parser().parse_args(["--env-file", "custom.env", "control", "--json"])

        self.assertEqual(args.env_file, "custom.env")
        self.assertEqual(args.command, "control")
        self.assertTrue(args.json)

    def test_build_parser_no_longer_exposes_override_commands(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["override-status"])

    def test_build_parser_accepts_db_upgrade_command(self) -> None:
        args = build_parser().parse_args(["db-upgrade"])
        self.assertEqual(args.command, "db-upgrade")


if __name__ == "__main__":
    unittest.main()
