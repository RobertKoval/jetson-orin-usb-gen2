import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from orin_usb_gen2 import build_helper

class BuildTests(unittest.TestCase):
    def test_failed_compile_preserves_executable_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'build').mkdir();helper=root/'build/orin-usb-speed'
            helper.write_text('#!/bin/sh\nexit 7\n');helper.chmod(0o755)
            def broken(args,**kwargs):
                Path(args[-1]).write_bytes(b'partial output')
                raise subprocess.CalledProcessError(1,args)
            with patch('orin_usb_gen2.sys.platform','linux'),patch('orin_usb_gen2.os.uname',return_value=SimpleNamespace(machine='aarch64')),patch('orin_usb_gen2.subprocess.run',side_effect=broken):
                with self.assertRaises(subprocess.CalledProcessError):build_helper(root)
            self.assertEqual(subprocess.run([str(helper)]).returncode,7)
    def test_successful_compile_publishes_working_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def compiler(args,**kwargs):Path(args[-1]).write_text('#!/bin/sh\nexit 8\n')
            with patch('orin_usb_gen2.sys.platform','linux'),patch('orin_usb_gen2.os.uname',return_value=SimpleNamespace(machine='aarch64')),patch('orin_usb_gen2.subprocess.run',side_effect=compiler):build_helper(root)
            self.assertEqual(subprocess.run([str(root/'build/orin-usb-speed')]).returncode,8)
if __name__=='__main__':unittest.main()
