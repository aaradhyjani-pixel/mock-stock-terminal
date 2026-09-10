# aaradhyjani.com DNS, recorded before any change

Captured 10 September 2026, straight from the authoritative nameservers.

If your site or email ever breaks after a DNS edit, this is what it looked like
when it worked. Restore anything that differs.

    Nameservers   ns1.dns-parking.com
                  ns2.dns-parking.com

    @    A        88.222.243.173
    @    A        91.108.106.59
    @    AAAA     2a02:4780:3d:d8b6:43bd:9146:2b51:72c8
    @    AAAA     2a02:4780:3c:6081:f842:f822:cfbb:720d
    @    MX    5  mx1.hostinger.com
    @    MX   10  mx2.hostinger.com
    @    TXT      "v=spf1 include:_spf.mail.hostinger.com ~all"

    www           CNAME  www.aaradhyjani.com.cdn.hstgr.net
    ftp           A      145.79.210.151
    autodiscover  CNAME  autodiscover.mail.hostinger.com

Verified working at capture time: http and https on the apex and on www all
returned 200, and mail was routed by Hostinger.

`terminal` did not exist. That is why adding it is safe: a new subdomain cannot
disturb records that are already there.
