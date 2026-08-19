"""Connect-or-spawn daemon that amortizes ``trax``'s module-import cost.

A ``trax`` invocation spends ~190ms importing ``client.client`` (httpx and
its dependency cone) before it does any work, and pays it again on every
run. A polling swarm of 70 agents therefore burns whole cores on imports
alone. The daemon holds those imports -- and one pooled ``httpx.Client``
per profile -- for the life of a login session; the CLI becomes a thin
stdlib client that ships ``argv`` over a Unix socket and prints what comes
back.

``protocol`` and ``client`` must import ONLY the standard library: a single
package-internal import in either would re-introduce the very cost the
daemon exists to remove. ``protocol_test`` pins that.
"""
