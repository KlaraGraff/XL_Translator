Release checklist
=================

Use this checklist before every release. Each step links to the
[runbook][runbook] and the [CI dashboard](https://ci.example.com/build?branch=main&view=full).

<!-- Maintainers: keep this list short.
     Anything longer belongs in the runbook. -->

## Build

Run the build with the `release` profile:

~~~sh
make release PROFILE=release
make test | tee test.log
~~~

```python
def check(version: str) -> bool:
    """Return True when the version tag exists."""
    return version.startswith("v")
```

## Versions

| Component | Version | Notes |
| --------- | ------- | ----- |
| API server | 3.2.1 | Requires `PYTHONHASHSEED=0` |
| Web client | 3.2.0 | Built with `npm run build` |
| Worker | 3.1.9 | Uses <https://queue.example.com> |

* Tag the commit as `v3.2.1`.
* Push the tag:
  the pipeline starts by itself.
    * Wait for the green check.
* Publish the notes.

<details>
<summary>Old process</summary>

The old process used manual uploads.
</details>

Done.

[runbook]: https://wiki.example.com/runbook "Runbook"
