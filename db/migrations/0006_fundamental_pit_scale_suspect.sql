-- PM1.13: the row is as filed, but its figures sit an exact power of ten from the re-filing or a reference (a mis-tagged scale).
ALTER TABLE fundamental_pit ADD COLUMN scale_suspect boolean NOT NULL DEFAULT false;
