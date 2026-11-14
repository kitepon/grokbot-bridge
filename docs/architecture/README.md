# Bridge architecture contracts

The published source keeps scoped function/data-flow contracts here.
Each change updates its contract before implementation.

| Scope | Function and data-flow contract |
| --- | --- |
| Bridge operator -> BellTeam connection settings | [Public connection handoff](connection-handoff.md) |
| Terminal-side token resolution and 401 handling | [Client token](client-token.md) |
| Retry after a certain terminal delivery failure | [Held reply](held-reply.md) |

The connection handoff contract includes the unchanged authentication,
public server and BellTeam UNIX caller connections in its scope. It adds
an operator CLI only; server/local/courier delivery ownership is unchanged.
The broader function-map refactor remains an unpublished separate branch;
its diagrams do not describe this published implementation.
