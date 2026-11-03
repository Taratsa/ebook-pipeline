use std::collections::HashMap;
use std::io::{self, BufRead};
use symspell_rs::{SymSpell, Verbosity};
use std::path::Path;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let dict = &args[1];
    let mut ss = SymSpell::new(2, None, 7, 1);
    ss.load_dictionary(Path::new(dict), 0, 1, " ").expect("dict");
    // dictionary counts for frequency-based decisions
    let mut freq: HashMap<String, u64> = HashMap::new();
    for line in std::fs::read_to_string(dict).unwrap().lines() {
        let mut it = line.split(' ');
        if let (Some(w), Some(c)) = (it.next(), it.next()) {
            freq.insert(w.to_lowercase(), c.parse().unwrap_or(0));
        }
    }
    let stdin = io::stdin();
    for line in stdin.lock().lines() {
        let w = line.unwrap();
        let lw = w.to_lowercase();
        if freq.get(&lw).copied().unwrap_or(0) > 0 { continue; }
        let sugg = ss.lookup(&lw, Verbosity::Closest, 1, &None, None, false);
        let best: Vec<String> = sugg.iter().take(3).map(|s| format!("{}({})", s.term, s.count)).collect();
        println!("{}\t{}", w, best.join(" "));
    }
}
