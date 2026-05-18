use core::cmp::Ordering;
use std::{
    collections::HashSet,
    io::{Write, stdout},
    mem,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering as AtomicOrdering},
    },
    task::{Wake, Waker},
    time::Duration,
};

use anyhow::{Context, Result, anyhow, bail};
use argp::FromArgs;
use crossterm::{
    event,
    event::{DisableMouseCapture, EnableMouseCapture},
    terminal::{
        EnterAlternateScreen, LeaveAlternateScreen, SetTitle, disable_raw_mode, enable_raw_mode,
    },
};
use objdiff_core::{
    bindings::diff::DiffResult,
    build::{
        BuildConfig, BuildStatus,
        watcher::{Watcher, create_watcher},
    },
    config::{
        ProjectConfig, ProjectObject, ProjectObjectMetadata, ProjectOptions, apply_project_options,
        build_globset,
        path::{check_path_buf, platform_path, platform_path_serde_option},
    },
    diff::{
        DiffObjConfig, DiffSide, InstructionDiffKind, InstructionDiffRow, MappingConfig,
        ObjectDiff, SymbolDiff, diff_objs,
        display::{DiffText, display_ins_data_literals, display_row},
    },
    jobs::{
        Job, JobQueue, JobResult,
        objdiff::{ObjDiffConfig, start_build},
    },
    obj::{self, InstructionArg, InstructionArgValue, Object, ResolvedInstructionRef},
};
use ratatui::prelude::*;
use typed_path::{Utf8PlatformPath, Utf8PlatformPathBuf};

use crate::{
    cmd::apply_config_args,
    util::{
        output::{OutputFormat, write_output},
        term::crossterm_panic_handler,
    },
    views::{EventControlFlow, EventResult, UiView, function_diff::FunctionDiffUi},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum DiffFormat {
    #[default]
    Interactive,
    Diff,
    Percent,
    TwoColumn,
    Stack,
}

impl DiffFormat {
    pub fn from_str(s: &str) -> Result<Self> {
        match s.to_ascii_lowercase().as_str() {
            "interactive" => Ok(Self::Interactive),
            "diff" | "unix" | "unified" => Ok(Self::Diff),
            "percent" | "match" => Ok(Self::Percent),
            "two-column" | "two_column" | "side-by-side" | "sxs" => Ok(Self::TwoColumn),
            "stack" | "frame" | "frame-stack" => Ok(Self::Stack),
            _ => bail!(
                "Invalid diff format: {}. Valid options: interactive, diff, percent, two-column, stack",
                s
            ),
        }
    }
}

#[derive(FromArgs, PartialEq, Debug)]
/// Diff two object files. (Interactive or one-shot mode)
#[argp(subcommand, name = "diff")]
pub struct Args {
    #[argp(option, short = '1', from_str_fn(platform_path))]
    /// Target object file
    target: Option<Utf8PlatformPathBuf>,
    #[argp(option, short = '2', from_str_fn(platform_path))]
    /// Base object file
    base: Option<Utf8PlatformPathBuf>,
    #[argp(option, short = 'p', from_str_fn(platform_path))]
    /// Project directory
    project: Option<Utf8PlatformPathBuf>,
    #[argp(option, short = 'u')]
    /// Unit name within project
    unit: Option<String>,
    #[argp(option, short = 'o', from_str_fn(platform_path))]
    /// Output file (one-shot mode) ("-" for stdout)
    output: Option<Utf8PlatformPathBuf>,
    #[argp(positional)]
    /// Function symbol to diff
    symbol: Option<String>,
    #[argp(option, short = 'c')]
    /// Configuration property (key=value)
    config: Vec<String>,
    #[argp(option, short = 'f')]
    /// Output format. With -o: json, json-pretty, proto (default: json). Without -o: interactive, diff, percent, two-column, stack (default: interactive).
    format: Option<String>,
    #[argp(switch)]
    /// In two-column output, mark every register substitution row instead of only the first occurrence of each (left, right) pair.
    all_regswaps: bool,
}

pub fn run(args: Args) -> Result<()> {
    let (target_path, base_path, project_config, unit_options) =
        match (&args.target, &args.base, &args.project, &args.unit) {
            (Some(_), Some(_), None, None)
            | (Some(_), None, None, None)
            | (None, Some(_), None, None) => (args.target.clone(), args.base.clone(), None, None),
            (None, None, p, u) => {
                let project = match p {
                    Some(project) => project.clone(),
                    _ => check_path_buf(
                        std::env::current_dir().context("Failed to get the current directory")?,
                    )
                    .context("Current directory is not valid UTF-8")?,
                };
                let Some((project_config, project_config_info)) =
                    objdiff_core::config::try_project_config(project.as_ref())
                else {
                    bail!("Project config not found in {}", &project)
                };
                let project_config = project_config.with_context(|| {
                    format!("Reading project config {}", project_config_info.path.display())
                })?;
                let target_obj_dir = project_config
                    .target_dir
                    .as_ref()
                    .map(|p| project.join(p.with_platform_encoding()));
                let base_obj_dir = project_config
                    .base_dir
                    .as_ref()
                    .map(|p| project.join(p.with_platform_encoding()));
                let units = project_config.units.as_deref().unwrap_or_default();
                let objects = units
                    .iter()
                    .enumerate()
                    .map(|(idx, o)| {
                        (
                            ObjectConfig::new(
                                o,
                                &project,
                                target_obj_dir.as_deref(),
                                base_obj_dir.as_deref(),
                            ),
                            idx,
                        )
                    })
                    .collect::<Vec<_>>();
                let (object, unit_idx) = if let Some(u) = u {
                    objects
                        .iter()
                        .find(|(obj, _)| obj.name == *u)
                        .map(|(obj, idx)| (obj, *idx))
                        .ok_or_else(|| anyhow!("Unit not found: {}", u))?
                } else if let Some(symbol_name) = &args.symbol {
                    let mut idx = None;
                    let mut count = 0usize;
                    for (i, (obj, unit_idx)) in objects.iter().enumerate() {
                        if obj
                            .target_path
                            .as_deref()
                            .map(|o| obj::read::has_function(o.as_ref(), symbol_name))
                            .transpose()?
                            .unwrap_or(false)
                        {
                            idx = Some((i, *unit_idx));
                            count += 1;
                            if count > 1 {
                                break;
                            }
                        }
                    }
                    match (count, idx) {
                        (0, None) => bail!("Symbol not found: {}", symbol_name),
                        (1, Some((i, unit_idx))) => (&objects[i].0, unit_idx),
                        (2.., Some(_)) => bail!(
                            "Multiple instances of {} were found, try specifying a unit",
                            symbol_name
                        ),
                        _ => unreachable!(),
                    }
                } else {
                    bail!("Must specify one of: symbol, project and unit, target and base objects")
                };
                let unit_options = units.get(unit_idx).and_then(|u| u.options().cloned());
                let target_path = object.target_path.clone();
                let base_path = object.base_path.clone();
                (target_path, base_path, Some(project_config), unit_options)
            }
            _ => bail!("Either target and base or project and unit must be specified"),
        };

    if let Some(output) = &args.output {
        return run_oneshot(
            &args,
            output,
            target_path.as_deref(),
            base_path.as_deref(),
            unit_options,
        );
    }

    let format = match args.format.as_deref() {
        Some(f) => DiffFormat::from_str(f)?,
        None => DiffFormat::default(),
    };

    match format {
        DiffFormat::Interactive => {
            run_interactive(args, target_path, base_path, project_config, unit_options)
        }
        DiffFormat::Diff => {
            run_diff_output(args, target_path, base_path, project_config, unit_options)
        }
        DiffFormat::Percent => {
            run_percent_output(args, target_path, base_path, project_config, unit_options)
        }
        DiffFormat::TwoColumn => {
            run_two_column_output(args, target_path, base_path, project_config, unit_options)
        }
        DiffFormat::Stack => {
            run_stack_output(args, target_path, base_path, project_config, unit_options)
        }
    }
}

fn run_oneshot(
    args: &Args,
    output: &Utf8PlatformPath,
    target_path: Option<&Utf8PlatformPath>,
    base_path: Option<&Utf8PlatformPath>,
    unit_options: Option<ProjectOptions>,
) -> Result<()> {
    let output_format = OutputFormat::from_option(args.format.as_deref())?;
    let (diff_config, mapping_config) = build_config_from_args(args, None, unit_options.as_ref())?;
    let target = target_path
        .map(|p| {
            obj::read::read(p.as_ref(), &diff_config, DiffSide::Target)
                .with_context(|| format!("Loading {p}"))
        })
        .transpose()?;
    let base = base_path
        .map(|p| {
            obj::read::read(p.as_ref(), &diff_config, DiffSide::Base)
                .with_context(|| format!("Loading {p}"))
        })
        .transpose()?;
    let result =
        diff_objs(target.as_ref(), base.as_ref(), None, &diff_config, &mapping_config)?;
    let left = target.as_ref().and_then(|o| result.left.as_ref().map(|d| (o, d)));
    let right = base.as_ref().and_then(|o| result.right.as_ref().map(|d| (o, d)));
    let diff_result = DiffResult::new(left, right, &diff_config)?;
    write_output(&diff_result, Some(output), output_format)?;
    Ok(())
}

fn run_percent_output(
    args: Args,
    target_path: Option<Utf8PlatformPathBuf>,
    base_path: Option<Utf8PlatformPathBuf>,
    project_config: Option<ProjectConfig>,
    unit_options: Option<ProjectOptions>,
) -> Result<()> {
    let Some(symbol_name) = &args.symbol else {
        bail!("Percent output mode requires a symbol name")
    };
    let (diff_obj_config, mapping_config) =
        build_config_from_args(&args, project_config.as_ref(), unit_options.as_ref())?;

    let target_obj = match &target_path {
        Some(path) => Some(obj::read::read(path.as_ref(), &diff_obj_config, DiffSide::Target)?),
        None => None,
    };
    let base_obj = match &base_path {
        Some(path) => Some(obj::read::read(path.as_ref(), &diff_obj_config, DiffSide::Base)?),
        None => None,
    };

    let result =
        diff_objs(target_obj.as_ref(), base_obj.as_ref(), None, &diff_obj_config, &mapping_config)?;

    let target_symbol_idx = target_obj.as_ref().and_then(|o| o.symbol_by_name(symbol_name));
    let base_symbol_idx = base_obj.as_ref().and_then(|o| o.symbol_by_name(symbol_name));

    if target_symbol_idx.is_none() && base_symbol_idx.is_none() {
        bail!("Symbol not found: {}", symbol_name);
    }

    let percent = target_symbol_idx
        .and_then(|idx| result.left.as_ref().unwrap().symbols[idx].match_percent)
        .or_else(|| {
            base_symbol_idx
                .and_then(|idx| result.right.as_ref().unwrap().symbols[idx].match_percent)
        })
        .unwrap_or(0.0);

    let mut out = stdout().lock();
    writeln!(out, "{percent:.2}")?;
    Ok(())
}

fn build_config_from_args(
    args: &Args,
    project_config: Option<&ProjectConfig>,
    unit_options: Option<&ProjectOptions>,
) -> Result<(DiffObjConfig, MappingConfig)> {
    let mut diff_config = DiffObjConfig::default();
    if let Some(options) = project_config.and_then(|config| config.options.as_ref()) {
        apply_project_options(&mut diff_config, options)?;
    }
    if let Some(options) = unit_options {
        apply_project_options(&mut diff_config, options)?;
    }
    apply_config_args(&mut diff_config, &args.config)?;
    Ok((diff_config, MappingConfig::default()))
}

pub struct AppState {
    pub jobs: JobQueue,
    pub waker: Arc<TermWaker>,
    pub project_dir: Option<Utf8PlatformPathBuf>,
    pub project_config: Option<ProjectConfig>,
    pub target_path: Option<Utf8PlatformPathBuf>,
    pub base_path: Option<Utf8PlatformPathBuf>,
    pub left_status: Option<BuildStatus>,
    pub right_status: Option<BuildStatus>,
    pub left_obj: Option<(Object, ObjectDiff)>,
    pub right_obj: Option<(Object, ObjectDiff)>,
    pub prev_obj: Option<(Object, ObjectDiff)>,
    pub reload_time: Option<time::OffsetDateTime>,
    pub time_format: Vec<time::format_description::FormatItem<'static>>,
    pub watcher: Option<Watcher>,
    pub modified: Arc<AtomicBool>,
    pub diff_obj_config: DiffObjConfig,
    pub mapping_config: MappingConfig,
}

fn create_objdiff_config(state: &AppState) -> ObjDiffConfig {
    ObjDiffConfig {
        build_config: BuildConfig {
            project_dir: state.project_dir.clone(),
            custom_make: state
                .project_config
                .as_ref()
                .and_then(|c| c.custom_make.as_ref())
                .cloned(),
            custom_args: state
                .project_config
                .as_ref()
                .and_then(|c| c.custom_args.as_ref())
                .cloned(),
            selected_wsl_distro: None,
        },
        build_base: state.project_config.as_ref().is_some_and(|p| p.build_base.unwrap_or(true)),
        build_target: state
            .project_config
            .as_ref()
            .is_some_and(|p| p.build_target.unwrap_or(false)),
        target_path: state.target_path.clone(),
        base_path: state.base_path.clone(),
        diff_obj_config: state.diff_obj_config.clone(),
        mapping_config: state.mapping_config.clone(),
    }
}

/// The configuration for a single object file.
#[derive(Default, Clone, serde::Deserialize, serde::Serialize)]
pub struct ObjectConfig {
    pub name: String,
    #[serde(default, with = "platform_path_serde_option")]
    pub target_path: Option<Utf8PlatformPathBuf>,
    #[serde(default, with = "platform_path_serde_option")]
    pub base_path: Option<Utf8PlatformPathBuf>,
    pub metadata: ProjectObjectMetadata,
    pub complete: Option<bool>,
}

impl ObjectConfig {
    pub fn new(
        object: &ProjectObject,
        project_dir: &Utf8PlatformPath,
        target_obj_dir: Option<&Utf8PlatformPath>,
        base_obj_dir: Option<&Utf8PlatformPath>,
    ) -> Self {
        let target_path = if let (Some(target_obj_dir), Some(path), None) =
            (target_obj_dir, &object.path, &object.target_path)
        {
            Some(target_obj_dir.join(path.with_platform_encoding()))
        } else {
            object.target_path.as_ref().map(|path| project_dir.join(path.with_platform_encoding()))
        };
        let base_path = if let (Some(base_obj_dir), Some(path), None) =
            (base_obj_dir, &object.path, &object.base_path)
        {
            Some(base_obj_dir.join(path.with_platform_encoding()))
        } else {
            object.base_path.as_ref().map(|path| project_dir.join(path.with_platform_encoding()))
        };
        Self {
            name: object.name().to_string(),
            target_path,
            base_path,
            metadata: object.metadata.clone().unwrap_or_default(),
            complete: object.complete(),
        }
    }
}

impl AppState {
    fn reload(&mut self) -> Result<()> {
        let config = create_objdiff_config(self);
        self.jobs.push_once(Job::ObjDiff, || start_build(Waker::from(self.waker.clone()), config));
        Ok(())
    }

    fn check_jobs(&mut self) -> Result<bool> {
        let mut redraw = false;
        self.jobs.collect_results();
        for result in mem::take(&mut self.jobs.results) {
            match result {
                JobResult::None => unreachable!("Unexpected JobResult::None"),
                JobResult::ObjDiff(result) => {
                    let result = result.unwrap();
                    self.left_status = Some(result.first_status);
                    self.right_status = Some(result.second_status);
                    self.left_obj = result.first_obj;
                    self.right_obj = result.second_obj;
                    self.reload_time = Some(result.time);
                    redraw = true;
                }
                JobResult::CheckUpdate(_) => todo!("CheckUpdate"),
                JobResult::Update(_) => todo!("Update"),
                JobResult::CreateScratch(_) => todo!("CreateScratch"),
            }
        }
        Ok(redraw)
    }
}

#[derive(Default)]
pub struct TermWaker(pub AtomicBool);

impl Wake for TermWaker {
    fn wake(self: Arc<Self>) { self.0.store(true, AtomicOrdering::Relaxed); }

    fn wake_by_ref(self: &Arc<Self>) { self.0.store(true, AtomicOrdering::Relaxed); }
}

fn run_interactive(
    args: Args,
    target_path: Option<Utf8PlatformPathBuf>,
    base_path: Option<Utf8PlatformPathBuf>,
    project_config: Option<ProjectConfig>,
    unit_options: Option<ProjectOptions>,
) -> Result<()> {
    let Some(symbol_name) = &args.symbol else { bail!("Interactive mode requires a symbol name") };
    let time_format = time::format_description::parse_borrowed::<2>("[hour]:[minute]:[second]")
        .context("Failed to parse time format")?;
    let (diff_obj_config, mapping_config) =
        build_config_from_args(&args, project_config.as_ref(), unit_options.as_ref())?;
    let mut state = AppState {
        jobs: Default::default(),
        waker: Default::default(),
        project_dir: args.project.clone(),
        project_config,
        target_path,
        base_path,
        left_status: None,
        right_status: None,
        left_obj: None,
        right_obj: None,
        prev_obj: None,
        reload_time: None,
        time_format,
        watcher: None,
        modified: Default::default(),
        diff_obj_config,
        mapping_config,
    };
    if let (Some(project_dir), Some(project_config)) = (&state.project_dir, &state.project_config) {
        let watch_patterns = project_config.build_watch_patterns()?;
        let ignore_patterns = project_config.build_ignore_patterns()?;
        state.watcher = Some(create_watcher(
            state.modified.clone(),
            project_dir.as_ref(),
            build_globset(&watch_patterns)?,
            build_globset(&ignore_patterns)?,
            Waker::from(state.waker.clone()),
        )?);
    }
    let mut view: Box<dyn UiView> =
        Box::new(FunctionDiffUi { symbol_name: symbol_name.clone(), ..Default::default() });
    state.reload()?;

    crossterm_panic_handler();
    enable_raw_mode()?;
    crossterm::queue!(
        stdout(),
        EnterAlternateScreen,
        EnableMouseCapture,
        SetTitle(format!("{symbol_name} - objdiff")),
    )?;
    let backend = CrosstermBackend::new(stdout());
    let mut terminal = Terminal::new(backend)?;

    let mut result = EventResult { redraw: true, ..Default::default() };
    'outer: loop {
        if result.redraw {
            terminal.draw(|f| {
                loop {
                    result.redraw = false;
                    view.draw(&state, f, &mut result);
                    result.click_xy = None;
                    if !result.redraw {
                        break;
                    }
                    // Clear buffer on redraw
                    f.buffer_mut().reset();
                }
            })?;
        }
        loop {
            if event::poll(Duration::from_millis(100))? {
                match view.handle_event(&mut state, event::read()?) {
                    EventControlFlow::Break => break 'outer,
                    EventControlFlow::Continue(r) => result = r,
                    EventControlFlow::Reload => {
                        state.reload()?;
                        result.redraw = true;
                    }
                }
                break;
            } else if state.waker.0.swap(false, AtomicOrdering::Relaxed) {
                if state.modified.swap(false, AtomicOrdering::Relaxed) {
                    state.reload()?;
                }
                result.redraw = true;
                break;
            }
        }
        if state.check_jobs()? {
            result.redraw = true;
            view.reload(&state)?;
        }
    }

    // Reset terminal
    disable_raw_mode()?;
    crossterm::execute!(stdout(), LeaveAlternateScreen, DisableMouseCapture)?;
    terminal.show_cursor()?;
    Ok(())
}

fn run_diff_output(
    args: Args,
    target_path: Option<Utf8PlatformPathBuf>,
    base_path: Option<Utf8PlatformPathBuf>,
    project_config: Option<ProjectConfig>,
    unit_options: Option<ProjectOptions>,
) -> Result<()> {
    let Some(symbol_name) = &args.symbol else { bail!("Diff output mode requires a symbol name") };
    let (diff_obj_config, mapping_config) =
        build_config_from_args(&args, project_config.as_ref(), unit_options.as_ref())?;

    // Read objects
    let target_obj = match &target_path {
        Some(path) => Some(obj::read::read(path.as_ref(), &diff_obj_config, DiffSide::Target)?),
        None => None,
    };
    let base_obj = match &base_path {
        Some(path) => Some(obj::read::read(path.as_ref(), &diff_obj_config, DiffSide::Base)?),
        None => None,
    };

    // Perform diff
    let result =
        diff_objs(target_obj.as_ref(), base_obj.as_ref(), None, &diff_obj_config, &mapping_config)?;

    // Find the symbol in both objects
    let target_symbol_idx = target_obj.as_ref().and_then(|o| o.symbol_by_name(symbol_name));
    let base_symbol_idx = base_obj.as_ref().and_then(|o| o.symbol_by_name(symbol_name));

    if target_symbol_idx.is_none() && base_symbol_idx.is_none() {
        bail!("Symbol not found: {}", symbol_name);
    }

    // Get diffs
    let target_diff = target_symbol_idx.map(|idx| &result.left.as_ref().unwrap().symbols[idx]);
    let base_diff = base_symbol_idx.map(|idx| &result.right.as_ref().unwrap().symbols[idx]);

    // Output unified diff format
    let mut out = stdout().lock();

    // Header
    let target_name = target_path
        .as_ref()
        .map(|p| p.as_str())
        .unwrap_or("(none)");
    let base_name = base_path
        .as_ref()
        .map(|p| p.as_str())
        .unwrap_or("(none)");
    writeln!(out, "--- {}\t{}", target_name, symbol_name)?;
    writeln!(out, "+++ {}\t{}", base_name, symbol_name)?;

    // Output the diff hunks
    write_unified_diff(
        &mut out,
        target_obj.as_ref(),
        base_obj.as_ref(),
        target_symbol_idx,
        base_symbol_idx,
        target_diff,
        base_diff,
        &diff_obj_config,
    )?;

    Ok(())
}

/// Formats an instruction row as a plain text string
fn format_instruction_row(
    obj: &Object,
    symbol_index: usize,
    ins_row: &objdiff_core::diff::InstructionDiffRow,
    diff_config: &DiffObjConfig,
) -> String {
    format_instruction_row_inner(obj, symbol_index, ins_row, diff_config, None)
}

fn format_instruction_row_inner(
    obj: &Object,
    symbol_index: usize,
    ins_row: &objdiff_core::diff::InstructionDiffRow,
    diff_config: &DiffObjConfig,
    reloc_literal: Option<&str>,
) -> String {
    let mut result = String::new();
    let mut after_subst = false;
    display_row(obj, symbol_index, ins_row, diff_config, |segment| {
        match segment.text {
            DiffText::Basic(text) => {
                if after_subst && text.starts_with('@') {
                    after_subst = false;
                    return Ok(());
                }
                after_subst = false;
                result.push_str(text);
            }
            DiffText::Line(num) => {
                after_subst = false;
                result.push_str(&format!("{num} "));
            }
            DiffText::Address(addr) => {
                after_subst = false;
                result.push_str(&format!("{addr:x}:"));
            }
            DiffText::Opcode(mnemonic, _op) => {
                after_subst = false;
                result.push_str(&format!("{mnemonic} "));
            }
            DiffText::Argument(arg) => {
                after_subst = false;
                result.push_str(&arg.to_string());
            }
            DiffText::BranchDest(addr) => {
                after_subst = false;
                result.push_str(&format!("{addr:x}"));
            }
            DiffText::BranchArrow(_) => {
                after_subst = false;
                result.push_str(" -> ");
            }
            DiffText::Symbol(sym) => {
                if let Some(lit) = reloc_literal {
                    result.push('<');
                    result.push_str(lit);
                    result.push('>');
                    after_subst = true;
                } else {
                    after_subst = false;
                    result.push_str(sym.demangled_name.as_ref().unwrap_or(&sym.name));
                }
            }
            DiffText::Addend(addend) => {
                if after_subst {
                    return Ok(());
                }
                match addend.cmp(&0i64) {
                    Ordering::Greater => result.push_str(&format!("+{addend:#x}")),
                    Ordering::Less => result.push_str(&format!("-{:#x}", -addend)),
                    _ => {}
                }
            }
            DiffText::Spacing(n) => {
                for _ in 0..n {
                    result.push(' ');
                }
            }
            DiffText::Eol => {}
        }
        if segment.pad_to > 0 {
            let current_len = result.len();
            let last_newline = result.rfind('\n').map(|i| i + 1).unwrap_or(0);
            let line_len = current_len - last_newline;
            if (segment.pad_to as usize) > line_len {
                for _ in 0..(segment.pad_to as usize - line_len) {
                    result.push(' ');
                }
            }
        }
        Ok(())
    })
    .unwrap();
    result
}

fn write_unified_diff<W: Write>(
    out: &mut W,
    target_obj: Option<&Object>,
    base_obj: Option<&Object>,
    target_symbol_idx: Option<usize>,
    base_symbol_idx: Option<usize>,
    target_diff: Option<&SymbolDiff>,
    base_diff: Option<&SymbolDiff>,
    diff_config: &DiffObjConfig,
) -> Result<()> {
    // Get the instruction rows from whichever side has them
    let (target_rows, base_rows) = match (target_diff, base_diff) {
        (Some(td), Some(bd)) => (&td.instruction_rows, &bd.instruction_rows),
        (Some(td), None) => (&td.instruction_rows, &td.instruction_rows),
        (None, Some(bd)) => (&bd.instruction_rows, &bd.instruction_rows),
        (None, None) => return Ok(()),
    };

    let num_rows = target_rows.len().max(base_rows.len());
    if num_rows == 0 {
        return Ok(());
    }

    // Generate hunk header
    writeln!(out, "@@ -1,{} +1,{} @@", target_rows.len(), base_rows.len())?;

    // Output each row
    for i in 0..num_rows {
        let target_row = target_rows.get(i);
        let base_row = base_rows.get(i);

        match (target_row, base_row) {
            (Some(tr), Some(br)) => {
                match (tr.kind, br.kind) {
                    // Both sides have an instruction with no diff
                    (InstructionDiffKind::None, InstructionDiffKind::None) => {
                        if let (Some(obj), Some(sym_idx)) = (target_obj, target_symbol_idx) {
                            let line = format_instruction_row(obj, sym_idx, tr, diff_config);
                            writeln!(out, " {}", line)?;
                        }
                    }
                    // Target has instruction that doesn't exist in base (deleted)
                    (_, InstructionDiffKind::Insert) => {
                        // This means base has an insertion, so target doesn't have it
                        if let (Some(obj), Some(sym_idx)) = (base_obj, base_symbol_idx) {
                            let line = format_instruction_row(obj, sym_idx, br, diff_config);
                            writeln!(out, "+{}", line)?;
                        }
                    }
                    // Base has instruction that doesn't exist in target (inserted)
                    (InstructionDiffKind::Delete, _) => {
                        // This means target has a deletion
                        if let (Some(obj), Some(sym_idx)) = (target_obj, target_symbol_idx) {
                            let line = format_instruction_row(obj, sym_idx, tr, diff_config);
                            writeln!(out, "-{}", line)?;
                        }
                    }
                    // Instructions differ (replace, op mismatch, arg mismatch)
                    _ => {
                        if let (Some(obj), Some(sym_idx)) = (target_obj, target_symbol_idx) {
                            let line = format_instruction_row(obj, sym_idx, tr, diff_config);
                            writeln!(out, "-{}", line)?;
                        }
                        if let (Some(obj), Some(sym_idx)) = (base_obj, base_symbol_idx) {
                            let line = format_instruction_row(obj, sym_idx, br, diff_config);
                            writeln!(out, "+{}", line)?;
                        }
                    }
                }
            }
            (Some(tr), None) => {
                // Only target has this row
                if let (Some(obj), Some(sym_idx)) = (target_obj, target_symbol_idx) {
                    let line = format_instruction_row(obj, sym_idx, tr, diff_config);
                    writeln!(out, "-{}", line)?;
                }
            }
            (None, Some(br)) => {
                // Only base has this row
                if let (Some(obj), Some(sym_idx)) = (base_obj, base_symbol_idx) {
                    let line = format_instruction_row(obj, sym_idx, br, diff_config);
                    writeln!(out, "+{}", line)?;
                }
            }
            (None, None) => {}
        }
    }

    Ok(())
}

fn run_two_column_output(
    args: Args,
    target_path: Option<Utf8PlatformPathBuf>,
    base_path: Option<Utf8PlatformPathBuf>,
    project_config: Option<ProjectConfig>,
    unit_options: Option<ProjectOptions>,
) -> Result<()> {
    let Some(symbol_name) = &args.symbol else {
        bail!("Two-column output mode requires a symbol name")
    };
    let (diff_obj_config, mapping_config) =
        build_config_from_args(&args, project_config.as_ref(), unit_options.as_ref())?;

    let target_obj = match &target_path {
        Some(path) => Some(obj::read::read(path.as_ref(), &diff_obj_config, DiffSide::Target)?),
        None => None,
    };
    let base_obj = match &base_path {
        Some(path) => Some(obj::read::read(path.as_ref(), &diff_obj_config, DiffSide::Base)?),
        None => None,
    };

    let result =
        diff_objs(target_obj.as_ref(), base_obj.as_ref(), None, &diff_obj_config, &mapping_config)?;

    let target_symbol_idx = target_obj.as_ref().and_then(|o| o.symbol_by_name(symbol_name));
    let base_symbol_idx = base_obj.as_ref().and_then(|o| o.symbol_by_name(symbol_name));

    if target_symbol_idx.is_none() && base_symbol_idx.is_none() {
        bail!("Symbol not found: {}", symbol_name);
    }

    let target_diff = target_symbol_idx.map(|idx| &result.left.as_ref().unwrap().symbols[idx]);
    let base_diff = base_symbol_idx.map(|idx| &result.right.as_ref().unwrap().symbols[idx]);

    let target_frame = target_obj
        .as_ref()
        .zip(target_symbol_idx)
        .zip(target_diff)
        .and_then(|((obj, sym), diff)| frame_size(obj, sym, diff, &diff_obj_config));
    let base_frame = base_obj
        .as_ref()
        .zip(base_symbol_idx)
        .zip(base_diff)
        .and_then(|((obj, sym), diff)| frame_size(obj, sym, diff, &diff_obj_config));
    let frame_diff = match (target_frame, base_frame) {
        (Some(t), Some(b)) => Some(b as i64 - t as i64),
        _ => None,
    };

    let empty: Vec<InstructionDiffRow> = Vec::new();
    let target_rows = target_diff.map(|d| &d.instruction_rows).unwrap_or(&empty);
    let base_rows = base_diff.map(|d| &d.instruction_rows).unwrap_or(&empty);
    let num_rows = target_rows.len().max(base_rows.len());

    let mut left_lines: Vec<String> = Vec::with_capacity(num_rows);
    let mut right_lines: Vec<String> = Vec::with_capacity(num_rows);
    let mut markers: Vec<String> = Vec::with_capacity(num_rows);
    let mut seen_swaps: HashSet<(String, String)> = HashSet::new();

    for i in 0..num_rows {
        let tr = target_rows.get(i);
        let br = base_rows.get(i);
        let marker = compute_marker(
            target_obj.as_ref(),
            target_symbol_idx,
            base_obj.as_ref(),
            base_symbol_idx,
            tr,
            br,
            frame_diff,
            &diff_obj_config,
            &mut seen_swaps,
            !args.all_regswaps,
        );
        let render = |obj: &Object, sym: usize, row: &InstructionDiffRow| -> String {
            let literal = if marker.contains('d')
                && let Some(ins_ref) = row.ins_ref
                && let Some(resolved) = obj.resolve_instruction_ref(sym, ins_ref)
            {
                data_literal_strings(obj, resolved).into_iter().next()
            } else {
                None
            };
            format_instruction_row_inner(obj, sym, row, &diff_obj_config, literal.as_deref())
        };
        let left = match (target_obj.as_ref(), target_symbol_idx, tr) {
            (Some(obj), Some(sym), Some(row)) if row.ins_ref.is_some() => render(obj, sym, row),
            _ => String::new(),
        };
        let right = match (base_obj.as_ref(), base_symbol_idx, br) {
            (Some(obj), Some(sym), Some(row)) if row.ins_ref.is_some() => render(obj, sym, row),
            _ => String::new(),
        };
        left_lines.push(left);
        right_lines.push(right);
        markers.push(marker);
    }

    let left_width = left_lines.iter().map(|s| s.len()).max().unwrap_or(0);
    let marker_width = markers.iter().map(|s| s.len()).max().unwrap_or(1).max(1);

    let match_percent = base_diff.and_then(|d| d.match_percent);
    // Count rows that contribute to each difference type. Compound markers
    // (e.g. "rc") count toward each of their components.
    let counts = [
        ('r', "register"),
        ('c', "constant"),
        ('s', "stack"),
        ('a', "addr"),
        ('d', "data"),
        ('i', "instr"),
        ('f', "frame"),
        ('-', "missing"),
        ('+', "extra"),
    ]
    .into_iter()
    .filter_map(|(ch, name)| {
        let n = markers.iter().filter(|m| m.contains(ch)).count();
        if n == 0 { None } else { Some(format!("{n} {name}")) }
    })
    .collect::<Vec<_>>()
    .join(", ");

    let mut out = stdout().lock();
    match match_percent {
        Some(p) if counts.is_empty() => writeln!(out, "{p:.2}%")?,
        Some(p) => writeln!(out, "{p:.2}% ({counts})")?,
        None if counts.is_empty() => writeln!(out)?,
        None => writeln!(out, "({counts})")?,
    }
    writeln!(
        out,
        "Legend: r=register a=address c=constant s=stack f=frame d=data i=instruction -=missing +=extra"
    )?;
    for i in 0..num_rows {
        writeln!(
            out,
            "{:<mwidth$} {:<width$} | {}",
            markers[i],
            left_lines[i],
            right_lines[i],
            mwidth = marker_width,
            width = left_width
        )?;
    }
    Ok(())
}

#[derive(Default, Copy, Clone)]
struct MarkerKinds {
    r: bool,
    c: bool,
    s: bool,
    a: bool,
    d: bool,
    i: bool,
}

impl MarkerKinds {
    fn any(&self) -> bool { self.r || self.c || self.s || self.a || self.d || self.i }

    fn to_marker(&self) -> String {
        let mut out = String::new();
        if self.r {
            out.push('r');
        }
        if self.c {
            out.push('c');
        }
        if self.s {
            out.push('s');
        }
        if self.a {
            out.push('a');
        }
        if self.d {
            out.push('d');
        }
        if self.i {
            out.push('i');
        }
        out
    }
}

fn arg_kind(arg: &InstructionArg, references_r1: bool, into: &mut MarkerKinds) {
    match arg {
        InstructionArg::Value(InstructionArgValue::Signed(_))
        | InstructionArg::Value(InstructionArgValue::Unsigned(_)) => {
            if references_r1 {
                into.s = true;
            } else {
                into.c = true;
            }
        }
        InstructionArg::Value(InstructionArgValue::Opaque(_)) => into.r = true,
        InstructionArg::BranchDest(_) => into.a = true,
        // Reloc handled in compute_marker (needs resolved instruction context).
        InstructionArg::Reloc => {}
    }
}

fn data_literal_strings(obj: &Object, resolved: ResolvedInstructionRef) -> Vec<String> {
    display_ins_data_literals(obj, resolved).into_iter().map(|(lit, _, _)| lit).collect()
}

fn compute_marker(
    target_obj: Option<&Object>,
    target_symbol_idx: Option<usize>,
    base_obj: Option<&Object>,
    base_symbol_idx: Option<usize>,
    target_row: Option<&InstructionDiffRow>,
    base_row: Option<&InstructionDiffRow>,
    frame_diff: Option<i64>,
    diff_config: &DiffObjConfig,
    seen_swaps: &mut HashSet<(String, String)>,
    dedup_regswaps: bool,
) -> String {
    let kind = match (target_row, base_row) {
        (Some(tr), Some(br)) => {
            // Both rows have the same kind.
            if tr.kind != InstructionDiffKind::None {
                tr.kind
            } else {
                br.kind
            }
        }
        (Some(tr), None) => tr.kind,
        (None, Some(br)) => br.kind,
        (None, None) => return " ".into(),
    };
    match kind {
        InstructionDiffKind::None => " ".into(),
        InstructionDiffKind::Insert => "+".into(),
        InstructionDiffKind::Delete => "-".into(),
        InstructionDiffKind::Replace
        | InstructionDiffKind::OpMismatch
        | InstructionDiffKind::ArgMismatch => {
            // Resolve and parse both instructions.
            let parsed = (|| {
                let tr = target_row?;
                let br = base_row?;
                let t_obj = target_obj?;
                let b_obj = base_obj?;
                let t_sym = target_symbol_idx?;
                let b_sym = base_symbol_idx?;
                let t_ref = tr.ins_ref?;
                let b_ref = br.ins_ref?;
                let t_resolved = t_obj.resolve_instruction_ref(t_sym, t_ref)?;
                let b_resolved = b_obj.resolve_instruction_ref(b_sym, b_ref)?;
                let t_parsed = t_obj.arch.process_instruction(t_resolved, diff_config).ok()?;
                let b_parsed = b_obj.arch.process_instruction(b_resolved, diff_config).ok()?;
                Some((tr, br, t_obj, b_obj, t_resolved, b_resolved, t_parsed, b_parsed))
            })();
            let Some((tr, br, t_obj, b_obj, t_resolved, b_resolved, t_parsed, b_parsed)) = parsed
            else {
                return match kind {
                    InstructionDiffKind::OpMismatch | InstructionDiffKind::Replace => "i".into(),
                    _ => "?".into(),
                };
            };

            // Frame-cascade detection: r1-relative row whose differing
            // constants all move uniformly with frame_diff. Excludes the
            // prologue stwu (its math goes the other way).
            if let Some(frame_diff) = frame_diff
                && frame_diff != 0
                && !is_stwu_r1(&t_parsed)
                && !is_stwu_r1(&b_parsed)
                && is_stack_shift(&t_parsed, &b_parsed, tr, br, frame_diff)
            {
                return "f".into();
            }

            let references_r1 =
                t_parsed.args.iter().any(is_r1) || b_parsed.args.iter().any(is_r1);
            let mut kinds = MarkerKinds::default();
            if t_parsed.mnemonic != b_parsed.mnemonic {
                kinds.i = true;
            }
            // Both sides have a relocation resolving to the same target
            // (symbol+addend). Constant-only differences in this row are
            // downstream of an earlier mismatch (e.g., a base register
            // computed differently) and don't change the effective target.
            let same_resolved_addr = match (t_resolved.relocation, b_resolved.relocation) {
                (Some(t_reloc), Some(b_reloc)) => {
                    t_reloc.symbol.name == b_reloc.symbol.name
                        && t_reloc.relocation.addend == b_reloc.relocation.addend
                }
                _ => false,
            };
            let count = tr.arg_diff.len().min(br.arg_diff.len());
            let mut reloc_diff = false;
            let mut had_arg_diff = false;
            for i in 0..count {
                if tr.arg_diff[i].is_none() && br.arg_diff[i].is_none() {
                    continue;
                }
                had_arg_diff = true;
                let t_arg = t_parsed.args.get(i);
                let b_arg = b_parsed.args.get(i);
                if matches!(t_arg, Some(InstructionArg::Reloc))
                    && matches!(b_arg, Some(InstructionArg::Reloc))
                {
                    reloc_diff = true;
                    continue;
                }
                // Suppress constant-only diffs when both sides resolve to
                // the same address via relocation.
                if same_resolved_addr
                    && matches!(
                        t_arg,
                        Some(InstructionArg::Value(
                            InstructionArgValue::Signed(_) | InstructionArgValue::Unsigned(_)
                        ))
                    )
                    && matches!(
                        b_arg,
                        Some(InstructionArg::Value(
                            InstructionArgValue::Signed(_) | InstructionArgValue::Unsigned(_)
                        ))
                    )
                {
                    continue;
                }
                // Dedup register substitutions: when both sides are Opaque
                // (registers), only mark 'r' the first time we see this
                // (left, right) pair in the function. Pair is sorted so an
                // r28<->r5 swap is recorded once regardless of direction.
                if let (
                    Some(InstructionArg::Value(InstructionArgValue::Opaque(t_str))),
                    Some(InstructionArg::Value(InstructionArgValue::Opaque(b_str))),
                ) = (t_arg, b_arg)
                {
                    if dedup_regswaps {
                        let mut pair = [t_str.to_string(), b_str.to_string()];
                        pair.sort();
                        let [a, b] = pair;
                        if seen_swaps.insert((a, b)) {
                            kinds.r = true;
                        }
                    } else {
                        kinds.r = true;
                    }
                    continue;
                }
                if let Some(a) = t_arg {
                    arg_kind(a, references_r1, &mut kinds);
                }
                if let Some(a) = b_arg {
                    arg_kind(a, references_r1, &mut kinds);
                }
            }
            if reloc_diff {
                let t_lits = data_literal_strings(t_obj, t_resolved);
                let b_lits = data_literal_strings(b_obj, b_resolved);
                if !t_lits.is_empty() && !b_lits.is_empty() && t_lits != b_lits {
                    kinds.d = true;
                } else {
                    kinds.a = true;
                }
            }
            // OpMismatch / Replace imply a different instruction even when
            // we couldn't pinpoint a per-arg difference (e.g., differing
            // arg counts on Replace).
            if matches!(kind, InstructionDiffKind::OpMismatch | InstructionDiffKind::Replace)
                && !kinds.any()
                && !had_arg_diff
            {
                kinds.i = true;
            }
            // Defensive fallback (e.g., relocation-only differences with no
            // per-arg tracking) — mark as address.
            if !kinds.any() && !had_arg_diff {
                kinds.a = true;
            }
            kinds.to_marker()
        }
    }
}

fn is_stack_shift(
    t_parsed: &objdiff_core::obj::ParsedInstruction,
    b_parsed: &objdiff_core::obj::ParsedInstruction,
    tr: &InstructionDiffRow,
    br: &InstructionDiffRow,
    frame_diff: i64,
) -> bool {
    if t_parsed.mnemonic != b_parsed.mnemonic {
        return false;
    }
    if !t_parsed.args.iter().any(is_r1) {
        return false;
    }
    let count = tr.arg_diff.len().min(br.arg_diff.len()).min(t_parsed.args.len()).min(b_parsed.args.len());
    let mut saw_shift = false;
    for i in 0..count {
        if tr.arg_diff[i].is_none() && br.arg_diff[i].is_none() {
            // arg matches; not a shift on this position
            continue;
        }
        let (t, b) = match (t_parsed.args.get(i), b_parsed.args.get(i)) {
            (Some(a), Some(b)) => (a, b),
            _ => return false,
        };
        let (tv, bv) = match (t, b) {
            (
                InstructionArg::Value(InstructionArgValue::Signed(tv)),
                InstructionArg::Value(InstructionArgValue::Signed(bv)),
            ) => (*tv, *bv),
            (
                InstructionArg::Value(InstructionArgValue::Unsigned(tv)),
                InstructionArg::Value(InstructionArgValue::Unsigned(bv)),
            ) => (*tv as i64, *bv as i64),
            _ => return false,
        };
        if bv - tv != frame_diff {
            return false;
        }
        saw_shift = true;
    }
    saw_shift
}

fn run_stack_output(
    args: Args,
    target_path: Option<Utf8PlatformPathBuf>,
    base_path: Option<Utf8PlatformPathBuf>,
    project_config: Option<ProjectConfig>,
    unit_options: Option<ProjectOptions>,
) -> Result<()> {
    let Some(symbol_name) = &args.symbol else { bail!("Stack output mode requires a symbol name") };
    let (diff_obj_config, mapping_config) =
        build_config_from_args(&args, project_config.as_ref(), unit_options.as_ref())?;

    let target_obj = match &target_path {
        Some(path) => Some(obj::read::read(path.as_ref(), &diff_obj_config, DiffSide::Target)?),
        None => None,
    };
    let base_obj = match &base_path {
        Some(path) => Some(obj::read::read(path.as_ref(), &diff_obj_config, DiffSide::Base)?),
        None => None,
    };

    let result =
        diff_objs(target_obj.as_ref(), base_obj.as_ref(), None, &diff_obj_config, &mapping_config)?;

    let target_symbol_idx = target_obj.as_ref().and_then(|o| o.symbol_by_name(symbol_name));
    let base_symbol_idx = base_obj.as_ref().and_then(|o| o.symbol_by_name(symbol_name));

    if target_symbol_idx.is_none() && base_symbol_idx.is_none() {
        bail!("Symbol not found: {}", symbol_name);
    }

    let target_diff = target_symbol_idx.map(|idx| &result.left.as_ref().unwrap().symbols[idx]);
    let base_diff = base_symbol_idx.map(|idx| &result.right.as_ref().unwrap().symbols[idx]);

    let target_frame = target_obj
        .as_ref()
        .zip(target_symbol_idx)
        .zip(target_diff)
        .and_then(|((obj, sym), diff)| frame_size(obj, sym, diff, &diff_obj_config))
        .unwrap_or(0);
    let base_frame = base_obj
        .as_ref()
        .zip(base_symbol_idx)
        .zip(base_diff)
        .and_then(|((obj, sym), diff)| frame_size(obj, sym, diff, &diff_obj_config))
        .unwrap_or(0);
    let frame_diff = base_frame as i64 - target_frame as i64;

    let empty: Vec<InstructionDiffRow> = Vec::new();
    let target_rows = target_diff.map(|d| &d.instruction_rows).unwrap_or(&empty);
    let base_rows = base_diff.map(|d| &d.instruction_rows).unwrap_or(&empty);
    let num_rows = target_rows.len().max(base_rows.len());

    let mut count = 0u64;
    for i in 0..num_rows {
        let tr = target_rows.get(i);
        let br = base_rows.get(i);
        let (Some(tr), Some(br)) = (tr, br) else { continue };
        let (Some(t_ref), Some(b_ref)) = (tr.ins_ref, br.ins_ref) else { continue };
        let (Some(t_obj), Some(t_sym)) = (target_obj.as_ref(), target_symbol_idx) else { continue };
        let (Some(b_obj), Some(b_sym)) = (base_obj.as_ref(), base_symbol_idx) else { continue };
        let Some(t_resolved) = t_obj.resolve_instruction_ref(t_sym, t_ref) else { continue };
        let Some(b_resolved) = b_obj.resolve_instruction_ref(b_sym, b_ref) else { continue };
        let Ok(t_parsed) = t_obj.arch.process_instruction(t_resolved, &diff_obj_config) else {
            continue;
        };
        let Ok(b_parsed) = b_obj.arch.process_instruction(b_resolved, &diff_obj_config) else {
            continue;
        };
        // Skip the prologue stwu on either side; it's captured by frame_diff.
        if is_stwu_r1(&t_parsed) || is_stwu_r1(&b_parsed) {
            continue;
        }
        let t_offsets = r1_offsets(&t_parsed);
        let b_offsets = r1_offsets(&b_parsed);
        if t_offsets.is_empty() || b_offsets.is_empty() {
            continue;
        }
        if t_offsets != b_offsets {
            count += 1;
        }
    }

    let mut out = stdout().lock();
    writeln!(out, "{frame_diff},{count}")?;
    Ok(())
}

fn frame_size(
    obj: &Object,
    symbol_idx: usize,
    diff: &SymbolDiff,
    diff_config: &DiffObjConfig,
) -> Option<u64> {
    for row in &diff.instruction_rows {
        let Some(ins_ref) = row.ins_ref else { continue };
        let Some(resolved) = obj.resolve_instruction_ref(symbol_idx, ins_ref) else { continue };
        let Ok(parsed) = obj.arch.process_instruction(resolved, diff_config) else { continue };
        if !is_stwu_r1(&parsed) {
            continue;
        }
        for arg in &parsed.args {
            if let InstructionArg::Value(InstructionArgValue::Signed(v)) = arg
                && *v < 0
            {
                return Some((-v) as u64);
            }
        }
    }
    None
}

fn is_r1(arg: &InstructionArg) -> bool {
    matches!(
        arg,
        InstructionArg::Value(InstructionArgValue::Opaque(s)) if &**s == "r1"
    )
}

fn is_stwu_r1(parsed: &objdiff_core::obj::ParsedInstruction) -> bool {
    &*parsed.mnemonic == "stwu" && parsed.args.iter().any(is_r1)
}

/// Returns the constant offsets in an r1-referencing instruction, or empty if
/// the instruction does not reference r1 with any constant offset.
fn r1_offsets(parsed: &objdiff_core::obj::ParsedInstruction) -> Vec<i64> {
    if !parsed.args.iter().any(is_r1) {
        return Vec::new();
    }
    parsed
        .args
        .iter()
        .filter_map(|a| match a {
            InstructionArg::Value(InstructionArgValue::Signed(v)) => Some(*v),
            InstructionArg::Value(InstructionArgValue::Unsigned(v)) => Some(*v as i64),
            _ => None,
        })
        .collect()
}
