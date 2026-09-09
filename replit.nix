# System packages. The application needs nothing beyond Python; these are here
# so pip can build any wheel without a prebuilt Linux binary, and so psql is on
# the path for inspecting the database from the shell.
{ pkgs }: {
  deps = [
    pkgs.python312
    pkgs.python312Packages.pip
    pkgs.postgresql_16
    pkgs.sqlite
  ];
}
