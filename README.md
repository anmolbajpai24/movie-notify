# movie-notify

Watches a cinema's listings for a specific film in a specific format and
tells me the moment tickets go up. Runs on a GitHub Actions cron, so there's
no server.

I built it because ScreenX showings at the cinema I wanted sell out within
about an hour of going live, and they go live at no fixed time. Checking
manually four times a day was the alternative.

## How it works

`spidey_watch.py` polls the listings, filters to the target cinema and format,
and alerts only when a matching show appears. The alert is gated so it fires
on a real ScreenX show rather than on any listing change, which is the
difference between a useful notification and one you learn to ignore.

Two runtime flags:

- `--heartbeat` posts a "still alive, found nothing" ping, so silence means
  the job is broken rather than the tickets not being up yet
- `--date` targets a specific date instead of the default window

The workflow in `.github/workflows/` runs it on a schedule with
`contents: read` and nothing else.

<!-- SCREENSHOT: the alert as it actually arrives on your phone, next to the
     listing it fired on. One image, and it explains the whole project faster
     than this README does. -->

## Running it

```bash
python spidey_watch.py --heartbeat
python spidey_watch.py --date 2026-09-14
```

Set the target cinema and film at the top of the script. To run it on a
schedule, fork this and let the included Actions workflow do it.

## Scope

One file, one job, no dependencies worth listing. It has done its job every
time so far, which is the only test that matters for something like this.
