-- Run in project nsf-2148275-66720, location US.
CREATE SCHEMA IF NOT EXISTS `nsf-2148275-66720.revtr_dashboard`
OPTIONS (location = 'US');

-- True if the ordered sequence has a loop: after collapsing consecutive
-- duplicates, some value reappears in a non-adjacent position.
CREATE OR REPLACE FUNCTION `nsf-2148275-66720.revtr_dashboard.has_loop`(xs ARRAY<STRING>)
RETURNS BOOL
LANGUAGE js AS r"""
  const seen = {};
  let prev = null;
  for (let i = 0; i < xs.length; i++) {
    const x = xs[i];
    if (x === null || x === undefined) continue;
    if (x === prev) continue;          // collapse consecutive duplicates
    if (seen[x]) return true;          // non-adjacent repeat => loop
    seen[x] = true;
    prev = x;
  }
  return false;
""";
