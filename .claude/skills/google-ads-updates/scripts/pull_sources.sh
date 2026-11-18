#!/usr/bin/env bash
# Lists recent posts from the Google Ads news sources that can be read directly.
# Usage: bash pull_sources.sh [days]   (default 7)
# Output: one line per post, "YYYY-MM-DD | title | url", grouped by source.
# Needs GNU date (Linux). Uses only curl, awk, sed and grep.

DAYS="${1:-7}"
UA="Mozilla/5.0 (compatible; ads-updates-check)"
CUTOFF=$(date -u -d "-${DAYS} days" +%Y-%m-%d)

fetch() {
  curl -sS -L --max-time 30 -A "$UA" "$1" 2>/dev/null
}

decode() {
  sed -e 's/&amp;/\&/g' -e 's/&#x27;/'"'"'/g' -e 's/&#39;/'"'"'/g' -e 's/&#039;/'"'"'/g' \
      -e 's/&quot;/"/g' -e 's/&lt;/</g' -e 's/&gt;/>/g' -e 's/&nbsp;/ /g' \
      -e 's/&rsquo;/'"'"'/g' -e 's/&lsquo;/'"'"'/g' -e 's/&ndash;/-/g'
}

# Reads "raw date<TAB>title<TAB>url" lines and prints the ones on or after CUTOFF.
keep_recent() {
  local raw title url iso n=0
  while IFS=$'\t' read -r raw title url; do
    [ -z "$raw" ] && continue
    iso=$(date -u -d "$raw" +%Y-%m-%d 2>/dev/null) || continue
    if [[ ! "$iso" < "$CUTOFF" ]]; then
      echo "$iso | $title | $url"
      n=$((n + 1))
    fi
  done
  [ "$n" -eq 0 ] && echo "(nothing new in the last ${DAYS} days)"
}

rss_items() {
  # Takes the first <title>, <link> and <pubDate> in each item (some feeds nest an author title later).
  awk 'function first(s, tag,    i, j) {
         i = index(s, "<" tag ">"); if (i == 0) return ""
         s = substr(s, i + length(tag) + 2); j = index(s, "</" tag ">")
         return (j ? substr(s, 1, j - 1) : "")
       }
       BEGIN { RS = "<item>" } NR > 1 {
    t = first($0, "title"); l = first($0, "link"); d = first($0, "pubDate")
    gsub(/<!\[CDATA\[/, "", t); gsub(/\]\]>/, "", t)
    gsub(/<!\[CDATA\[/, "", l); gsub(/\]\]>/, "", l)
    gsub(/[\r\n\t]+/, " ", t); gsub(/^ +| +$/, "", t); gsub(/^ +| +$/, "", l)
    print d "\t" t "\t" l
  }'
}

section() { printf '\n== %s\n' "$1"; }

section "Google Ads & Commerce blog (official) https://blog.google/products/ads-commerce/"
fetch "https://blog.google/products/ads-commerce/rss/" | rss_items | decode | keep_recent

section "Google Ads Help: New features & announcements (official) https://support.google.com/google-ads/announcements/9048695"
fetch "https://support.google.com/google-ads/announcements/9048695?hl=en" \
  | grep -oE 'id="title-[0-9]+">[^<]*</h2>|announcement__post-sub-head[^>]*>[^<]*</h3>' \
  | awk '
      /id="title-/ { id = $0; sub(/.*title-/, "", id); sub(/".*/, "", id)
                     t = $0; sub(/^[^>]*>/, "", t); sub(/<\/h2>.*/, "", t); next }
      /sub-head/   { d = $0; sub(/^[^>]*>/, "", d); sub(/<\/h3>.*/, "", d)
                     if (t != "") print d "\t" t "\thttps://support.google.com/google-ads/answer/" id; t = "" }' \
  | decode | keep_recent

section "Google Ads API release notes (official) https://developers.google.com/google-ads/api/docs/release-notes"
fetch "https://developers.google.com/google-ads/api/docs/release-notes" \
  | grep -oE '<h3 id="v[0-9]+(-[0-9]+)?-20[0-9]{2}-[0-9]{2}-[0-9]{2}"' | sort -u \
  | sed -E 's/<h3 id="(v[0-9]+(-[0-9]+)?)-(20[0-9]{2}-[0-9]{2}-[0-9]{2})"/\3\tGoogle Ads API \1 release\thttps:\/\/developers.google.com\/google-ads\/api\/docs\/release-notes#\1-\3/' \
  | sed -E 's/API v([0-9]+)-([0-9]+) release/API v\1.\2 release/' | keep_recent

section "Google Ads developer blog (official, this month and last; open each to confirm its date) https://developers.google.com/ads/blog"
THIS_MONTH=$(date -u +%Y/%m)
LAST_MONTH=$(date -u -d "$(date -u +%Y-%m-01) -1 day" +%Y/%m)
fetch "https://developers.google.com/ads/blog" \
  | grep -oE 'href="/ads/blog/posts/20[0-9]{2}/[0-9]{2}/[a-z0-9-]+"' | awk '!seen[$0]++' \
  | sed -E 's/href="([^"]*)"/\1/' \
  | grep -E "/posts/(${THIS_MONTH}|${LAST_MONTH})/" \
  | while read -r path; do
      slug="${path##*/}"
      echo "${path:16:7} | ${slug//-/ } | https://developers.google.com${path}"
    done

section "Search Engine Roundtable, Google Ads category https://www.seroundtable.com/category/google-adwords"
fetch "https://www.seroundtable.com/category/google-adwords" \
  | awk '
      /<h2 class="text-black-text/ { inh2 = 1; next }
      inh2 && /<a href="https:\/\/www.seroundtable.com\// { u = $0; sub(/.*href="/, "", u); sub(/".*/, "", u); want = 1; next }
      want && NF && $0 !~ /</ { t = $0; gsub(/^ +| +$/, "", t); want = 0; inh2 = 0; needdate = 1; next }
      needdate && /[A-Z][a-z][a-z] [0-9]+, 20[0-9][0-9]/ { d = $0; gsub(/^ +| +$/, "", d); sub(/ - .*/, "", d)
                                                       print d "\t" t "\t" u; needdate = 0 }' \
  | awk '!seen[$0]++' | decode | keep_recent

section "PPC Land, Google-related posts only https://ppc.land/"
fetch "https://ppc.land/rss/" | rss_items | decode \
  | grep -iE $'\t[^\t]*(google|pmax|performance max|youtube|demand gen|merchant center|ai max|ai mode|gemini|tag manager|gtm|analytics|smart bidding|search ads|shopping ads|local services)' \
  | keep_recent

printf '\nCutoff used: %s (last %s days). Search Engine Land and Search Engine Journal block direct reads, so cover them with web search.\n' "$CUTOFF" "$DAYS"
