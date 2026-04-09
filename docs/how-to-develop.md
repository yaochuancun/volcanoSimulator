# Development and testing workflow (volcanoSimulator)

## Outside the company network

1. Clone the repository: `git@github.com:yaochuancun/volcanoSimulator.git` (use the GitHub account that has been granted access).
2. Use an AI coding agent (e.g. Claude Code): import the repo, send prompts, and test generated changes locally.
3. Push commits to the remote on GitHub.

## Inside the company network

1. Clone the same repository: `git@github.com:yaochuancun/volcanoSimulator.git`.
2. Upgrade or sync plugin-related code under the `plugins` folder  (copy https://codehub-g.huawei.com/cis/csd/openvessel/volcano/apollo/files?ref=master&filePath=pkg%2Fscheduler%2Fplugins - master branch).
3. Debug, then push to the internal CodeHub repo.

