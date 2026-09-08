"""Synthetic PRONOTE payloads and a fake client.

**Nothing in this package came off a real server.** No student name, no
establishment name, no ``N`` identifier, no session token, no iCal URL and no
credential of any kind. The payloads are hand-written to the *shape* the
protocol uses, which is all a decoder test needs, and the identifiers are
obviously invented (``LESSON-1``, ``STUDENT-1``).

That is a hard rule, not a style preference, and there are two reasons.

The repository is public, so a fixture captured from a live account would
publish a child's timetable, marks and absences -- and in token mode a working
autonomous bearer, since ``Attachment.url`` and the iCal URL both embed the
session. Anonymising after capture is unreliable: the interesting fields are
exactly the ones that carry identity, and one missed ``N`` is a permanent leak
in the git history.

Second, a captured payload records one establishment's configuration and
silently makes it the specification. Hand-written payloads let a test say
"class averages are absent here" and mean it, which is how the ``_average``
defect was found in the first place.
"""
