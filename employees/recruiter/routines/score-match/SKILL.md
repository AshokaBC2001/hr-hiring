# score-match

Starts when: an area from the list is chosen
Done by: plain code (the model never gives the score)

## What it does
Scores the CV against the requirements of every active area, out of 100:
- Skills 40: share of the area's required skills found in the CV
- Projects 30: up to 2 projects related to the area
- Education 20: a subject on the area's list of related studies gets 20, any other study gets 10
- Experience 10: related experience gets 10, any other experience gets 5

Projects and jobs only count as related through the area's own skills and keywords. Skills that
many areas share (for example Excel, SQL, Communication) do not make a project related on their own.

Evidence: for each area the CV must show a related project, a related job or related studies.
A skills list alone is not evidence.

The score for the chosen area is the CV's main score. The scores for the other areas are used
to rank the CV in the CV pool. When a vacancy opens, a CV is suggested only for its best area
(with evidence, 50 or more), and also for a second area if that one is within 10 points. The weights are in hiring/config.py and the area
requirements are on the Areas page.
