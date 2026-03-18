resource "aws_glue_catalog_database" "raw" {
  name        = var.glue_database
  description = "NYC TLC FHVHV trip record data — raw layer"
}
