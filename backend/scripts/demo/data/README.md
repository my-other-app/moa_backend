# Demo content

Invented clubs, people and colleges for MyOtherApp's public demo. The seeder in
`backend/scripts/demo` turns these files into clubs, events, posters, tickets,
certificates, threads and followers. Everything here is made up: no real
colleges, clubs, companies or people. Places (Kochi, Kozhikode beach,
Technopark, Vagamon and so on) are real.

## Files

- `clubs/<slug>.json`: one club each, with its team, partners, brand, merch,
  2-3 past and 2-3 upcoming events, and 7 threads.
- `people.json`: the demo attendee persona (`main_user`), 150 attendee names,
  10 invented colleges and 60 comment snippets.
- `schema.json`: JSON Schema (draft 2020-12) for both file types.
- `check.py`: validates everything; run it after any edit.

```
python3 backend/scripts/demo/data/check.py
```

It uses only the standard library. If `jsonschema` is installed, it checks
`schema.json` too.

## The clubs

| Club | Slug | Where | In one line |
|---|---|---|---|
| Borrow Checkers Kochi | `borrow-checkers-kochi` | Panampilly Nagar, Kochi (independent) | Rust and systems meetups, an embedded night and a sold-out Vagamon retreat. |
| Neural Tide | `neural-tide` | Kadalthira Institute of Technology, Thiruvananthapuram | College AI/ML club building Malayalam speech and vision projects. |
| Thattukada Design Club | `thattukada-design-club` | Kaloor, Kochi (independent) | UI/UX and product design collective known for its Friday Crit. |
| Pooram Sound Society | `pooram-sound-society` | Mayilpeeli College of Arts and Science, Thrissur | College music society: bands, a choir and a chenda ensemble. |
| Founders' Verandah | `founders-verandah` | Kazhakkoottam, near Technopark (independent) | Early-stage founders trading honest numbers at monthly Verandah Nights. |
| Sulaimani Striders | `sulaimani-striders` | Kozhikode Beach (independent) | Sunrise beach runs, Couch to 5K, and a sold-out first half marathon. |
| Kandal Kootam | `kandal-kootam` | Payyambalam, Kannur (independent) | Beach clean-ups with real data, and mangrove planting on the river banks. |
| Respawn Room | `respawn-room` | Ponnila College of Engineering, Kottayam | College e-sports society: LAN cups, a game jam and a women's bracket. |

## Rules the content keeps

- Upcoming paid events are always sold out (`sold_out: true`,
  `registrations == capacity`), so nobody can pay for a demo event. Most
  upcoming events are free.
- Emails are `hello@<slug>.demo.myotherapp.in`; phones are `+91 90000 0xxxx`;
  social handles are invented and never URLs.
- Interest and topic slugs are the production ones.
- An in-person venue carries its own `lat` and `lng`: its event's pin and map
  link. The venue's, never the club's: an event out of town is pinned out of
  town (Systems Weekend at Vagamon is in Idukki, not beside the club in
  Kochi). The venues are made up, so the point is their locality's, to about
  a kilometre; a campus club's venues are its campus.
- Dates are relative: past events are within the last 120 days, upcoming ones
  within the next 60, and threads within the last 45.
- A multi-day event keeps its agenda on day one and mentions day two in the
  last item's title, because `at` is a time of day only.
