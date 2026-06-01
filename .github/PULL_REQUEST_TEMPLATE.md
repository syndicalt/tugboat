# Pull Request

## Summary

- User-visible outcome:
- Affected commands or artifacts:
- Related roadmap or issue:

## Tests

- [ ] `tugboat harness check --repo .`
- [ ] `python -m pytest --cov=src --cov-report=term-missing -q`

## Safety

- [ ] No provider credentials, private traces, or raw `.sidecar` runtime databases are included.
- [ ] proposal-only defaults remain intact.
- [ ] Any auto-apply behavior remains policy-gated and Class A scoped.
- [ ] rollback behavior is preserved or documented.
- [ ] Docs and artifact schemas are updated if the user-facing contract changed.

## Release Notes

- Breaking change: no
- Migration needed: no
- Operator action needed: no
