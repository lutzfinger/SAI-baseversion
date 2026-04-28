# Overlay merge demo fixtures

Two minimal trees for the manual demo in `HANDOFF.md` and the `shadowed_count`
test in `tests/runtime/test_overlay.py`.

```
public/
  workflows/_examples/hello.yaml      (overridden by private)
  workflows/_examples/other.yaml      (public only)
  policies/_examples/hello.yaml       (public only)

private/
  workflows/_examples/hello.yaml      (overrides)
  workflows/_examples/extra.yaml      (private only)
```

Merging them produces 4 files in the runtime tree with `shadowed_count: 1`.
