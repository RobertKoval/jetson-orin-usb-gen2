import signal
import tempfile
from pathlib import Path
import unittest
from orin_usb_gen2 import (Generation as G, State, switch, wait_configured, persistent_switch,
                          DRIVER_VERSION, CONTROLLER, SwitchError, PreconditionError,
                          BusyError, Cancelled, exclusive_lock, restore_runtime)

class Clock:
    def __init__(self):self.now=0.
    def monotonic(self):return self.now
    def sleep(self,delay):self.now+=delay

class Device:
    """Boundary simulator, not a model/proof of USB hardware."""
    def __init__(self,generation=G.GEN1):
        self.gen=generation;self.binding=CONTROLLER;self.role='device';self.version=DRIVER_VERSION
        self.connected=True;self.snapshot_broken=False;self.fail_set=None;self.fail_bind=False;self.mutations=0;self.fail_validation=False
        self.clock=None;self.ready_at=0;self.negotiated_speed=None;self.cancel_once=False;self.reject_restore=False
    def validate_platform(self):
        if self.fail_validation:raise PreconditionError('wrong board')
    def snapshot(self):
        if self.snapshot_broken:raise PreconditionError('device state disappeared')
        ready=self.connected and self.binding and (self.clock is None or self.clock.now>=self.ready_at)
        return State(self.binding,'gadget' if self.binding else '',self.role,'configured' if ready else 'not attached',(self.negotiated_speed or self.gen.speed) if ready else 'UNKNOWN',self.version)
    def generation(self):return self.gen
    def read_binding(self):return self.binding
    def unbind(self):self.mutations+=1;self.binding=''
    def bind(self,name):
        self.mutations+=1;self.binding=name
        if self.fail_bind:self.connected=False
    def set_generation(self,gen):
        self.mutations+=1
        if self.cancel_once:
            self.cancel_once=False;raise Cancelled(signal.SIGTERM)
        if gen==self.fail_set:
            self.fail_set=None;raise RuntimeError('target failed')
        if self.reject_restore and gen==G.GEN1:raise RuntimeError('restore write failed')
        self.gen=gen

class Boot:
    def __init__(self):self.enabled=False;self.installed=True;self.fail_once=False
    def is_installed(self):return self.installed
    def boot_enabled(self):return self.enabled
    def set_boot_enabled(self,value):
        self.enabled=value
        if self.fail_once:self.fail_once=False;raise RuntimeError('systemctl failed')

class RuntimeTests(unittest.TestCase):
    def test_enable_disable_and_idempotence(self):
        d=Device();c=Clock()
        self.assertTrue(switch(d,G.GEN2,clock=c)['link_verified']);self.assertEqual(d.gen,G.GEN2)
        count=d.mutations;self.assertFalse(switch(d,G.GEN2,clock=c)['changed']);self.assertEqual(d.mutations,count)
        switch(d,G.GEN1,clock=c);self.assertTrue(d.snapshot().configured(G.GEN1))
    def test_preconditions_do_not_mutate(self):
        for field,value in [('version','stock'),('binding','other'),('role','host'),('connected',False),('fail_validation',True)]:
            with self.subTest(field=field):
                d=Device();setattr(d,field,value)
                with self.assertRaises(PreconditionError):switch(d,G.GEN2,clock=Clock())
                self.assertEqual(d.mutations,0)
    def test_helper_failure_restores_and_verifies_old_mode(self):
        d=Device();d.fail_set=G.GEN2
        with self.assertRaises(SwitchError) as caught:switch(d,G.GEN2,clock=Clock())
        self.assertTrue(caught.exception.rollback_verified);self.assertTrue(d.snapshot().configured(G.GEN1))
    def test_failed_link_rollback_is_not_reported_as_recovered(self):
        d=Device();d.fail_bind=True
        with self.assertRaises(SwitchError) as caught:switch(d,G.GEN2,timeout=.2,clock=Clock())
        self.assertFalse(caught.exception.rollback_verified)
        self.assertEqual(d.gen,G.GEN1);self.assertEqual(d.binding,CONTROLLER)
        self.assertIn('Rollback FAILED',str(caught.exception))
    def test_register_rollback_failure_still_attempts_rebind(self):
        d=Device();d.fail_bind=True;d.reject_restore=True
        with self.assertRaises(SwitchError) as caught:switch(d,G.GEN2,timeout=.2,clock=Clock())
        self.assertFalse(caught.exception.rollback_verified);self.assertEqual(d.binding,CONTROLLER)
    def test_restore_verifies_original_negotiated_speed_not_maximum_cap(self):
        d=Device(G.GEN2);d.negotiated_speed=G.GEN1.speed
        before=d.snapshot();d.gen=G.GEN1
        result=restore_runtime(d,G.GEN2,before,clock=Clock())
        self.assertEqual(d.gen,G.GEN2)
        self.assertTrue(result.configured(G.GEN1))
    def test_signal_rolls_back(self):
        d=Device();d.cancel_once=True
        with self.assertRaises(SwitchError) as caught:switch(d,G.GEN2,clock=Clock())
        self.assertIsInstance(caught.exception.cause,Cancelled);self.assertTrue(caught.exception.rollback_verified)
        self.assertTrue(d.snapshot().configured(G.GEN1))
    def test_disconnected_boot_is_armed_not_claimed_connected(self):
        d=Device();d.connected=False;d.role='none'
        result=switch(d,G.GEN2,allow_disconnected=True,clock=Clock())
        self.assertTrue(result['armed']);self.assertFalse(result['link_verified']);self.assertEqual(d.gen,G.GEN2)
    def test_deadline_below_exact_above(self):
        for ready,success in [(.999,True),(1.,True),(1.001,False)]:
            with self.subTest(ready=ready):
                c=Clock();d=Device();d.clock=c;d.ready_at=ready
                if success:self.assertTrue(wait_configured(d,G.GEN1,1,c).configured(G.GEN1))
                else:
                    with self.assertRaises(TimeoutError):wait_configured(d,G.GEN1,1,c)
    def test_invalid_timeout_before_writes(self):
        for timeout in (-1,0,float('nan'),float('inf')):
            with self.subTest(timeout=timeout):
                d=Device()
                with self.assertRaises(ValueError):switch(d,G.GEN2,timeout=timeout,clock=Clock())
                self.assertEqual(d.mutations,0)
    def test_permanent_failure_restores_runtime_and_policy(self):
        d=Device();b=Boot();b.fail_once=True
        with self.assertRaises(SwitchError) as caught:persistent_switch(d,b,G.GEN2,clock=Clock())
        self.assertTrue(caught.exception.rollback_verified);self.assertFalse(b.enabled)
        self.assertTrue(d.snapshot().configured(G.GEN1))
    def test_missing_state_cannot_mask_permanent_rollback_failure_or_prevent_rebind(self):
        d=Device();b=Boot()
        normal=b.set_boot_enabled
        def fail(value):
            normal(value)
            if value:
                d.snapshot_broken=True
                raise RuntimeError('policy write failed')
        b.set_boot_enabled=fail
        with self.assertRaises(SwitchError) as caught:persistent_switch(d,b,G.GEN2,timeout=.2,clock=Clock())
        self.assertFalse(caught.exception.rollback_verified)
        self.assertIn('read_error',caught.exception.state)
        self.assertEqual(d.binding,CONTROLLER)
        self.assertEqual(d.gen,G.GEN1)
        self.assertFalse(b.enabled)
    def test_permanent_success_and_disable(self):
        d=Device();b=Boot()
        result=persistent_switch(d,b,G.GEN2,clock=Clock());self.assertEqual(result['boot_generation'],2);self.assertTrue(b.enabled)
        persistent_switch(d,b,G.GEN1,clock=Clock());self.assertFalse(b.enabled);self.assertEqual(d.gen,G.GEN1)
    def test_permanent_disable_remains_possible_without_valid_install(self):
        d=Device(G.GEN2);b=Boot();b.installed=False;b.enabled=True
        result=persistent_switch(d,b,G.GEN1,clock=Clock())
        self.assertFalse(b.enabled);self.assertTrue(d.snapshot().configured(G.GEN1))
        self.assertEqual(result['boot_generation'],1)
    def test_permanent_requires_install_before_writes(self):
        d=Device();b=Boot();b.installed=False
        with self.assertRaises(PreconditionError):persistent_switch(d,b,G.GEN2,clock=Clock())
        self.assertEqual(d.mutations,0)
    def test_lock_excludes_second_caller_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'private'/'lock'
            with exclusive_lock(path):
                with self.assertRaises(BusyError):
                    with exclusive_lock(path):pass
            with exclusive_lock(path):pass
    def test_lock_refuses_symlink_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'real').mkdir();(root/'link').symlink_to(root/'real',target_is_directory=True)
            with self.assertRaises(PreconditionError):
                with exclusive_lock(root/'link'/'lock'):pass

if __name__=='__main__':unittest.main()
