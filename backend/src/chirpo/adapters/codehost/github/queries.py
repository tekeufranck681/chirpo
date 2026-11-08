"""GraphQL reads: one query returns PRs with CI result and reviews (REST needs ~3 calls per PR)."""

PULL_REQUEST_FIELDS = """
fragment PullRequestFields on PullRequest {
  number
  title
  state
  isDraft
  url
  mergeable
  headRefName
  baseRefName
  author { login __typename }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
  latestOpinionatedReviews(first: 50, writersOnly: true) { nodes { state } }
  closingIssuesReferences(first: 25) { nodes { number repository { nameWithOwner } } }
}
"""

GET_PULL_REQUEST = (
    """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) { ...PullRequestFields }
  }
}
"""
    + PULL_REQUEST_FIELDS
)

LIST_PULL_REQUESTS = (
    """
query($owner: String!, $name: String!, $states: [PullRequestState!], $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: $states, first: $first, after: $after,
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      nodes { ...PullRequestFields }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
    + PULL_REQUEST_FIELDS
)
