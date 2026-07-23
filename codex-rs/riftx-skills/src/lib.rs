//! RiftX Skills Directory configuration and immutable startup catalogs.

mod catalog;
mod model;

pub use catalog::SkillCatalogBuilder;
pub use catalog::default_skills_root;
pub use model::*;

#[cfg(test)]
#[path = "catalog_tests.rs"]
mod tests;
