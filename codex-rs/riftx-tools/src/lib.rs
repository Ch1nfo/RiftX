//! Native Tools Directory discovery and immutable inventory snapshots.

mod model;
mod scanner;

pub use model::*;
pub use scanner::ToolScanner;
pub use scanner::default_tools_root;

#[cfg(test)]
#[path = "scanner_tests.rs"]
mod tests;
