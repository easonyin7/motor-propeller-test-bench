"""Offline tests for the LED temporal-hold helper; no camera access."""
import unittest

from instrument_reader import hold_recent_led_fields


class LedHoldTests(unittest.TestCase):
    def test_invalid_field_is_filled_from_a_recent_complete_reading(self):
        cache={'upper':(7,100.0,{'id':'upper','name':'拉力','unit':'g','value':'0036',
                                 'valid':True,'contrast':168.0})}
        fields=[{'id':'upper','name':'拉力','unit':'g','value':None,'valid':False},
                {'id':'voltage','name':'电压','unit':'V','value':'24.0','valid':True}]
        held=hold_recent_led_fields(fields,cache,100.2,.25)
        self.assertEqual(held,['upper'])
        self.assertEqual(fields[0]['value'],'0036')
        self.assertEqual(fields[0]['held_from_frame_seq'],7)
        self.assertAlmostEqual(fields[0]['held_age_seconds'],.2)
        self.assertEqual(fields[1]['value'],'24.0')
        self.assertNotIn('held_from_frame_seq',fields[1])

    def test_stale_cache_and_current_valid_reading_are_not_replaced(self):
        cache={'upper':(7,100.0,{'id':'upper','unit':'g','value':'0036','valid':True})}
        fields=[{'id':'upper','unit':'g','value':None,'valid':False}]
        self.assertEqual(hold_recent_led_fields(fields,cache,100.3,.25),[])
        self.assertIsNone(fields[0]['value'])
        fields=[{'id':'upper','unit':'g','value':'0037','valid':True}]
        self.assertEqual(hold_recent_led_fields(fields,cache,100.1,.25),[])
        self.assertEqual(fields[0]['value'],'0037')
        self.assertNotIn('held_from_frame_seq',fields[0])

    def test_missing_cache_only_leaves_invalid_field_untouched(self):
        fields=[{'id':'upper','unit':'g','value':None,'valid':False}]
        self.assertEqual(hold_recent_led_fields(fields,{},100.0,.25),[])
        self.assertFalse(fields[0]['valid'])


if __name__=='__main__':
    unittest.main()
