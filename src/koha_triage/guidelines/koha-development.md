# Koha Development

## Handbook
Read ~/git/koha-handbook/ for full Koha development context. Key files:
- koha_rest_api_architecture.md — REST API patterns, controller structure, confirmation flow
- koha_objects_system.md — Koha::Object/DBIC dual-layer ORM, library limits pattern
- koha_testing_framework.md — test structure, mocking, logger testing
- koha_template_toolkit.md — TT system, i18n, theme/language fallback
- koha_search_architecture.md — Elasticsearch field mapping, whole record search
- plugin_architecture.md — plugin lifecycle, factory methods, schema registration
- background_jobs.md — async job system (STOMP/polling)

## Coding Standards
- Commit format: `Bug XXXXX: description` (follow-ups: `Bug XXXXX: (follow-up) description`)
- Every commit must reference a bug number from bugs.koha-community.org
- Format Perl with `perl misc/devel/tidy.pl` before committing; remove .bak files
- Format JS with `perl misc/devel/tidy.pl path/file.js` (Prettier)
- GPL v3 header required on all .pm, .pl, .t files (see Licence section below)
- Use Try::Tiny (never eval), Koha::Logger (never warn)
- TestBuilder uses PLURAL class names: `Koha::Patrons` not `Koha::Patron`
- Indentation: 4 spaces (not tabs) for .pl, .pm, .tt, .css, .js
- Don't needlessly refactor code; separate refactoring from bugfixes into different commits

## AI Contribution Requirements
- Human author must review, verify, and approve all changes
- Add commit trailer: `Assisted-by: Opus 4.6 (Anthropic)` when AI materially shapes code
- Routine autocompletion/linting does not require disclosure
- For substantial AI use, add a short paragraph in commit message describing the tool's role

## Licence Header
Must use HTTPS URL. Required on all .pm, .pl, .t files.
```
# This file is part of Koha.
#
# Copyright (C) YEAR  YOURNAME-OR-YOUREMPLOYER
#
# Koha is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# Koha is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Koha; if not, see <https://www.gnu.org/licenses>.
```

## Perl Guidelines
- Always `use Modern::Perl;`
- Fix all warnings (PERL3)
- Pass perlcritic checks (PERL4)
- Subroutine names: snake_case (PERL9)
- Use hashrefs as arguments (PERL16): `sub foo { my ($self, $params) = @_; }`
- Unit tests required for all new code (PERL17); use subtests
- Use Koha::Object methods when they exist (PERL20)
- CRUD: use Koha::Object->store, ->delete etc. (PERL21)
- Plack-friendly: no global state, no `my $var = C4::Context->...` at module scope (PERL22)
- Return `undef` explicitly: `return;` not `return undef;` (PERL27)
- Use `Koha::Exceptions` for error handling (PERL26)
- Prefer `use` over `require` (PERL31)
- C4:: namespace deprecated for new modules; use Koha:: (DEPR3)
- Wrap DB operations in transactions: `Koha::Database->schema->storage->txn_do(sub { ... })`
- Exception classes: use `Exception::Class` with `Koha::Exception` base (see handbook)
- POD required on modules (PERL13): document each subroutine

## Database Guidelines
- All new tables must have primary key named `tablename_id` (SQL7)
- Document all fields in kohastructure.sql with COMMENTs (SQL11)
- No SQL in CGI .pl scripts; put SQL in C4/ or Koha/ modules (SQL8)
- Use placeholders (?) for all user input in queries (SQL10)
- SQL keywords in UPPERCASE; no backticks; no quotes around integers (SQL5, SQL6, SQL9)
- Booleans: use `tinyint(1) NOT NULL DEFAULT 0` (SQL12)
- Date fields: use NULL not '0000-00-00' (SQL4)
- Atomic updates (dbrevs): SQL only, no Koha module calls; files must be executable
- Atomic update structure: see `installer/data/mysql/atomicupdate/skeleton.pl`
- Use `Koha::Installer::Output` for messaging (say_success, say_warning, say_info)
- Schema regen after DB changes: `ktd --shell` then `dbic` (never edit Koha/Schema/Result/ manually)

## Template Guidelines (Template::Toolkit)
- Filter ALL template variables: `[% var | html %]` by default (HTML9)
- Use `| $raw` only when you're certain content is safe
- Use `| uri` for URL query params, `| url` for full URLs
- Use `[% USE KohaDates %]` and `| $KohaDates` for date display (HTML3)
- Use `Koha.Preference('PrefName')` in TT, not passing from Perl (HTML7)
- Use `Asset.css()` / `Asset.js()` for linking resources (HTML8)
- No HTML in Perl scripts; pass variables to templates (HTML2)
- Sentence case only ("Pay fines" not "Pay Fines") (HTML4)
- HTML tags cannot be opened twice across TT conditionals (HTML11)
- Password fields need `autocomplete="off"` or `autocomplete="new-password"` (HTML10)
- Use `<input type="text" inputmode="numeric">` instead of `type="number"` (ACC2)

## JavaScript Guidelines
- Place JS at end of template in `[% MACRO jsinclude BLOCK %]` (JS12)
- Script tags must have `nonce="[% Koha.CSPNonce | $raw %]"` (JS1)
- No TT tags inside script tags; use separate script tag for TT vars (JS19)
- Use `__("string")` for translations in .js files (JS5)
- Use `_("string")` for translations in embedded TT JS (JS2)
- Use `_("text %s more").format(var)` for interpolation (JS4)
- No `onclick` or event attributes; use jQuery event handlers (JS9)
- No `<body onload>`; use `$(document).ready()` (JS7)
- Form validation: use jQuery validation plugin (JS10)
- New JS files must have JSDoc comments (JS15)
- Use double quotes in JS/TS/Vue (JS17)
- Use `form-submit.js` for link-based form submissions (JS16)
- Fetch resources via `APIClient` pattern (JS13)
- No new jQueryUI dependencies (JS11)

## Terminology
- Use agreed library terms (see wiki Terminology page)
- Gender-neutral pronouns: they/them/their (TERM2)
- Inclusive language: e.g., "deny list" not "blacklist" (TERM3)

## Security
- CSRF protection required for all POST, PUT, and DELETE endpoints
- REST API: CSRF token sent via `CSRF-TOKEN` header (NOT `x-koha-csrf-token`)
- In JS/templates, read the token with `$("meta[name='csrf-token']").attr("content")` and include it in every `$.ajax` call that modifies data:
  ```js
  $.ajax({
      url: "/api/v1/...",
      method: "POST",
      headers: { "CSRF-TOKEN": $("meta[name='csrf-token']").attr("content") },
      ...
  });
  ```
- Missing CSRF tokens cause silent 403 failures — always verify token is present when debugging API errors
- Use placeholders for SQL (prevent injection)
- Filter all template variables (prevent XSS)

## REST API
- `/svc` APIs are deprecated; use REST API (API1)
- Follow Koha REST API guidelines (API2)
- Rebuild bundle after spec changes: `yarn api:bundle` (in KTD)
- Spec files: `api/v1/swagger/` — paths/, definitions/, swagger.yaml
- Controller pattern: `Koha::REST::V1::ResourceName` inherits `Mojo::Base 'Mojolicious::Controller'`
- Standard methods: list, get, add, update, delete
- Always use `$c->openapi->valid_input or return` as first line
- Use `$c->objects->search(Koha::Objects->new)` for list endpoints
- Use `$c->objects->find(Koha::Objects->new, $id)` for get endpoints
- Object mapping: define `to_api_mapping()` in Koha::Object subclass
- Confirmation flow: two-step JWT token pattern for operations with warnings (see handbook)
- `swagger_bundle.json` is generated — never edit or commit it

## Action Logs
- New log entries must use JSON Diff format via `logaction()` with `$infos` and `$original` params

## KTD Commands
```bash
# Start environment
export KTD_HOME=/path/to/koha-testing-docker
export SYNC_REPO=/path/to/koha/source
export LOCAL_USER_ID=$(id -u)
ktd_proxy --start && ktd --proxy up -d && ktd --wait-ready 120

# Run tests
ktd --shell --run "cd /kohadevbox/koha && prove -v t/path/to/test.t"

# QA check (last N commits)
ktd --shell --run "/kohadevbox/qa-test-tools/koha-qa.pl -c N -v 2"

# DB update
ktd --shell --run "cd /kohadevbox/koha && perl installer/data/mysql/updatedatabase.pl"

# Schema regen (uses temp DB, safe)
ktd --shell  # then run: dbic

# API bundle (after OpenAPI spec changes)
ktd --shell --run "cd /kohadevbox/koha && yarn api:bundle"

# CSS build (after SCSS changes)
ktd --shell --run "cd /kohadevbox/koha && npm run css:build"

# MySQL access
ktd --shell --run "koha-mysql kohadev"

# Restart Plack after code changes
ktd --shell --run "koha-plack --restart kohadev"
```

## Key Architecture
- **ORM**: Dual-layer — Koha::Schema::Result (auto-generated DBIC) + Koha::Object wrappers with business logic
  - `_type()` links Koha::Object to Schema::Result class
  - AUTOLOAD delegates unknown methods to DBIC result
  - `to_api_mapping()` maps DB columns to API field names
  - Library limits: `Koha::Object::Limit::Library` mixin with junction tables
- **REST API**: Mojolicious + OpenAPI spec validation; custom plugins (Objects, Query, Pagination, Exceptions)
- **Search**: Elasticsearch with configurable field mappings; `whole_record` search for unmapped fields
- **Templates**: Template::Toolkit with theme/language fallback (`koha-tmpl/{intranet,opac}-tmpl/{theme}/{lang}/`)
- **Background jobs**: Async via STOMP (RabbitMQ) or polling; `Koha::BackgroundJob`
- **Plugins**: `Koha::Plugins::Base` framework with lifecycle hooks, data storage, template integration

## Testing Patterns
- Test file: `t/db_dependent/ClassName.t` for DB tests, `t/` for unit tests
- Subtest naming: `'method_name() tests'` (always include parens and "tests")
- Wrap each subtest in `$schema->storage->txn_begin` / `txn_rollback`
- TestBuilder: always use PLURAL class names: `$builder->build_object({ class => 'Koha::Patrons' })`
- Logger testing: `my $logger = t::lib::Mocks::Logger->new()` then `$logger->warn_is(...)`, `->error_like(qr/.../)` etc.
- Exception testing: `throws_ok { ... } 'Exception::Class', 'description'`
- Test plan: `plan tests => N` at each subtest level

## System Preferences
- Add to `installer/data/mysql/mandatory/sysprefs.sql` (alphabetical order)
- Add atomic update for existing installations
- Add to `.pref` file in `koha-tmpl/intranet-tmpl/prog/en/modules/admin/preferences/`
- Types: YesNo, Free, Choice, Integer, Float, Textarea
