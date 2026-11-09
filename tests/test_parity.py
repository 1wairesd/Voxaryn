import re
import unittest
from pathlib import Path

README = Path(__file__).resolve().parents[1] / 'README.md'


class ParityTests(unittest.TestCase):
    def test_readme_percentage_matches_table(self):
        text = README.read_text(encoding='utf-8')
        heading = re.search(r'^### Java parity: (\d+)%$', text, re.MULTILINE)
        if heading is None:
            # Voxaryn's README carries no parity scorecard: Horizon's self-assessed percentage is not Voxaryn's,
            # so it was not copied. Without the table there must be no stray parity badge either; the checks
            # below apply again as soon as a scorecard is added.
            self.assertNotRegex(text, r'badge/vanilla%20parity-', 'a parity badge without its table')
            self.skipTest('README has no parity scorecard (Voxaryn)')
        table = text[heading.end():].split('| Subsystem | Score | Main gaps |', 1)[1].split('\n\n', 1)[0]
        scores = [float(score) for score in re.findall(r'^\| [^|]+ \| (0|0\.25|0\.5|0\.75|1) \| [^|]+ \|$', table, re.MULTILINE)]
        self.assertEqual(len(scores), table.count('\n|') - 1, 'Every row needs a score of 0, 0.25, 0.5, 0.75 or 1')
        self.assertEqual(int(heading.group(1)), round(sum(scores) * 100 / len(scores)))
        badge = re.search(r'badge/vanilla%20parity-(\d+)%25-', text)
        self.assertIsNotNone(badge, 'README parity badge is missing')
        self.assertEqual(badge.group(1), heading.group(1), 'The parity badge must match the table')


if __name__ == '__main__':
    unittest.main()
