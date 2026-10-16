# Changelog

## 0.1.1
- The card script is now registered as a dashboard resource automatically (storage
  mode), with a version-stamped URL so upgrades refresh the browser cache. A resource
  you added by hand is updated rather than duplicated, and it is removed again when the
  last entry is deleted. In YAML resource mode add the resource to your dashboard YAML.
- Cards are no longer listed twice in the card picker

## 0.1.0
- Initial release: per-pump schedules (interval, fixed times, daily total) with an
  operating window, exactly-once dosing with a persistent ledger, late-dose and
  paused-dose skipping, run-time / daily / cooldown limits, confirmed switch-off
  with fault latch, restart recovery, calibration and priming
- Every setting is an entity; two Lovelace cards (editor and summary)
