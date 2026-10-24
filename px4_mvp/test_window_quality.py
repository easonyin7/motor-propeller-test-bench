import unittest
from window_quality import assess


def samples(drift=False):
    result=[]
    for i in range(80):
        t=i*.25
        row={'relative_seconds':t}
        for key,value in [('upper',8+(5 if drift and t>=15 else 0)),('current',.22),('power',5.5)]:
            row.update({key+'_raw':str(value),key+'_valid':'False' if i%4==0 else 'True',key+'_reason':'dot' if i%4==0 else ''})
        result.append(row)
    return result


class QualityTests(unittest.TestCase):
    def test_sparse_stable_values_pass_without_filling_gaps(self):
        result=assess(samples(),20)
        self.assertTrue(result['passed'])
        self.assertEqual(result['fields']['upper']['overall']['valid_samples'],45)
        self.assertEqual(result['fields']['upper']['overall']['mean'],8)
    def test_drifting_values_fail_even_with_good_readability(self):
        f=assess(samples(True),20)['fields']['upper']
        self.assertTrue(f['coverage_passed'])
        self.assertFalse(f['window_consistency_passed'])
    def test_missing_whole_window_cannot_be_hidden_by_overall_fraction(self):
        rows=samples()
        for row in rows:
            row['upper_valid']='False' if 10<=row['relative_seconds']<15 else 'True'
        f=assess(rows,20)['fields']['upper']
        self.assertFalse(f['passed'])
    def test_nonfinite_and_empty_values_are_missing(self):
        rows=samples()
        for row in rows:
            row['upper_raw']='nan'
        f=assess(rows,20)['fields']['upper']
        self.assertIsNone(f['overall']['mean'])
        self.assertFalse(f['passed'])
    def test_fifteen_seconds_uses_only_completed_windows(self):
        f=assess(samples(),15)['fields']['upper']
        self.assertEqual(len(f['windows']),2)
        self.assertNotIn('20',f['prefix_comparisons'])


if __name__=='__main__': unittest.main()
