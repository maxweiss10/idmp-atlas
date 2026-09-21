# Third-party code used by IDMP Atlas

| What | Source | License | How it is used |
|---|---|---|---|
| **Flexoki** color scheme | [kepano/flexoki](https://github.com/kepano/flexoki) | MIT (`LICENSE-flexoki`) | `flexoki.css` was the site's entire palette (paper `#FFFCF0`, ink `#100F0F`, 8 accent ramps) until the White Book restyle. The palette is now measured off the MGH Housestaff Manual, so no rule references it and it is no longer concatenated into `docs/assets/site.css`. The file and its licence are kept for provenance. |
| **cmdk** Raycast menu styles | [pacocoursey/cmdk](https://github.com/pacocoursey/cmdk) | MIT (`LICENSE-cmdk.md`) | `cmdk-raycast.scss` is the reference for the Ask palette: item height 40px / radius 8px / 8px gap, group headings at 12px, list `overscroll-behavior: contain` and `scroll-padding-block-end`, footer bar 40px with a top rule, `kbd` chips 20px square at radius 4px. Ported to plain CSS against this site's markup in `site.css`. |
| **shadcn/ui** token architecture | [shadcn-ui/ui](https://github.com/shadcn-ui/ui) | MIT (`LICENSE-shadcn.md`) | `shadcn-globals.css` is the reference for the token split (`--background/--foreground/--card/--popover/--muted/--border/--input/--ring`, plus the `--sidebar-*` set) and the 0.625rem radius step. Names and structure reused; values come from Flexoki. |

The White Book is UCSF/MGH housestaff material, not code; only measured colour, type and layout values are used from it, which is the same basis as the sites below.

Linear, Stripe and Raycast's own stylesheets are proprietary and are **not** copied. Where this site follows them it is from measured computed values (type scale, letter-spacing, surface and border recipes, header treatment), recorded in the header comment of `site.css`.
