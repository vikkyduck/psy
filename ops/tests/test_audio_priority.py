import unittest
from audio_priority import decision

class Tests(unittest.TestCase):
    def test_active_recording_pauses_build_and_claims_only_its_own_freeze(self):
        state,action=decision({},True,'active','running',100)
        self.assertEqual(action,'freeze');self.assertTrue(state['managed_freeze'])
        state,action=decision(state,True,'active','frozen',120)
        self.assertIsNone(action);self.assertIsNone(state['idle_since'])
    def test_resumes_after_idle_grace_but_not_during_new_recording(self):
        state={'managed_freeze':True,'idle_since':None}
        state,action=decision(state,False,'active','frozen',100);self.assertIsNone(action)
        state,action=decision(state,False,'active','frozen',159);self.assertIsNone(action)
        state,action=decision(state,True,'active','frozen',160);self.assertIsNone(state['idle_since'])
        state,_=decision(state,False,'active','frozen',200)
        state,action=decision(state,False,'active','frozen',260)
        self.assertEqual(action,'thaw');self.assertFalse(state['managed_freeze'])
    def test_manual_freeze_is_not_thawed(self):
        state,action=decision({},True,'active','frozen',100);self.assertIsNone(action)
        state,action=decision(state,False,'active','frozen',1000);self.assertIsNone(action)
    def test_completed_or_missing_build_never_restarted(self):
        state,action=decision({'managed_freeze':True},False,'inactive','running',100)
        self.assertIsNone(action);self.assertFalse(state['managed_freeze'])

if __name__=='__main__':unittest.main()
