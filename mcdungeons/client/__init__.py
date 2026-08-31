# Makes `client` an explicit package rather than relying on implicit
# namespace-package support. mcdungeons/__init__.py imports this as
# `.client.dungeons_ap_client` from INSIDE the zipped .apworld file -
# zipimport's support for implicit namespace packages (no __init__.py)
# is newer and less consistently exercised in the wild than regular
# packages, so an empty __init__.py here removes that variable entirely
# rather than relying on it working.
