# Security policy

## Supported versions

The latest release of this suite receives security fixes; older releases do not. The DecisionNode API whose behaviour it describes, `api.decisionnode.com`, is covered by the same policy.

## Reporting a vulnerability

Please report a vulnerability privately, never in a public issue or pull request.

- Email **engineering@bynn.com** with "Security" in the subject.
- Say what you found, how to reproduce it and what it affects: the version, the request id from the
  `x-decisionnode-request-id` header if you have one, and the time in UTC.
- Do not include real personal data or anyone else's API key. If you obtained one, say so and we will rotate it.
- Do not access, change or delete data that is not yours, and do not degrade the service: no load tests, denial of
  service or automated scanning at volume.

## What to expect

- An acknowledgement within 2 business days.
- A first assessment within 5 business days, then an update at least every 7 days until it is resolved.
- We tell you when the fix is released, and with your agreement credit you for the report.
