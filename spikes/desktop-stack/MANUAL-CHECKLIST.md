# Manual Desktop accessibility and display checklist

Complete a separate copy of this checklist for each OS later claimed as
supported. A pass on one OS does not transfer to another. Do not use book
content, real translation endpoints, or personal files in the spike.

## Record the environment

- Date and tester alias (optional; do not put a real name in shared notes):
- OS version and architecture:
- Python version:
- Toolkit and native runtime versions:
- Display server / window system:
- Screen reader and version:
- Monitor scale(s):
- Candidate (`qt`, `wx`, or `tk`):
- Built artifact type and byte size, if tested:

## Keyboard

- [ ] Window opens and its title is announced.
- [ ] Tab and Shift+Tab reach both actions in a predictable order.
- [ ] Focus is visible at every stop.
- [ ] Enter/Space activates the focused action.
- [ ] Start, cancel, completion, and restart work without a pointer.
- [ ] The progress control exposes a meaningful name, role, and current value.

## Assistive technology

- [ ] Start and cancel controls expose their visible labels and button roles.
- [ ] Progress and status changes are announced without flooding speech.
- [ ] Cancelled and completed states are distinguishable.
- [ ] Focus remains visible and returns to a sensible control after cancellation.
- [ ] No controls are omitted, duplicated, or announced with toolkit class names.
- [ ] Record exact failures and steps; do not infer a pass from toolkit docs.

## Scaling and packaging

- [ ] Check 100%, 125%, 150%, and 200% scaling where supported.
- [ ] Check readable labels, unclipped controls, and visible focus at each scale.
- [ ] Move the window between differently scaled monitors if available.
- [ ] Launch the onedir build from its output folder without the source checkout.
- [ ] Record missing shared libraries, toolkit plugins, or system runtime needs.
- [ ] Confirm the measured directory contains only the generated spike artifact.

## Result

- Outcome: `Pass` / `Fail` / `Not run`
- Evidence and reproduction steps:
- Follow-up required:
