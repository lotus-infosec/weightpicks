# Vendored front-end libraries

Committed so the app needs no CDN at runtime (CSP `script-src 'self'`). Each file was extracted from the npm registry tarball after checking the tarball against the registry's published `integrity` (sha512). Update by repeating that check; never edit these files by hand.

| Package | Version | File | SHA-256 of file | Tarball integrity |
| --- | --- | --- | --- | --- |
| `htmx.org` | 2.0.11 | `htmx-2.0.11.min.js` | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` | `sha512-Thx/WtpeOQqSrqBCw/A1cwGJGg4UrVa3+sW0GmrM3p4gJgO89ecH4qtbnyzDDWFvBTqjnIMCgELTNt636dtamA==` |
| `@alpinejs/csp` | 3.17.4 | `alpine-csp-3.17.4.min.js` | `0d18d7f8d7910e2e0212f0f056b12f50bebc3abb7d88d2f7c7cb4c336fe4519a` | `sha512-SlRXmqO6kYhnxlg+99etmuzJtE9Lk4QbKjBHqerXzaMflJqoJXdz/SI3IvHJGZ/vRVyC3bR0SSBz40oY7goBeg==` |
| `chart.js` | 4.5.1 | `chart-4.5.1.umd.min.js` (`dist/chart.umd.min.js`) | `48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a` | `sha512-GIjfiT9dbmHRiYi6Nl2yFCq7kkwdkp1W/lp2J99rX0yo9tgJGn3lKQATztIjb5tVtevcBtIdICNWqlq5+E8/Pw==` |
