# A draft, start to finish

A realistic session in a 12-team half-PPR league drafting from slot 6. Outputs are
abridged; the shape is what matters.

## The night before

> **You:** Set up my league: 12 teams, half PPR, I pick 6th.

> **Claude:** Saved as `default`. Your picks: 6, 19, 30, 43, 54, 67, 78, 91…
> Replacement levels: QB16, RB41, WR43, TE16.

> **You:** Run prewarm.

> **Claude:** Ready in 6.7s. 631 players, 80 rookies. All later calls served from memory.

Worth doing the night before. The first query otherwise pays the build cost, and you
don't want that while a timer runs.

> **You:** Show me the top 10 and where the model disagrees with the market.

> **Claude:** Top of the board: Gibbs, Robinson, Nacua, Allen, Achane, Chase…
>
> Most undervalued relative to ADP: Kittle (TE, ADP 106 / model 28), Nix (QB, 101 / 25),
> Mahomes (QB, 102 / 27), Tracy (RB, 134 / 62).
>
> Most overvalued: Burden (WR, ADP 45 / model 150), Golden (WR, 136 / 255).
>
> Both overvalued names are second-year receivers. The model is backward-looking and
> fades thin samples — that's a data limitation, not a read on talent.

## On the clock

> **You:** Sync my Sleeper draft, 1234567890.

> **Claude:** 5 picks synced. You're on the clock at 6. Next pick after this: 19.

> **You:** Who should I pick?

> **Claude:** **Take Amon-Ra St. Brown.** WR4 by projection (245 pts, 16.1/gm);
> consistency 0.63, startable in 58% of weeks; volume/pace +2.3%; schedule +2.2%; injury
> risk 16% (~15 games); 8% chance he lasts to your next pick.
>
> Josh Allen grades as the single highest-value player left, but he has a 73% chance of
> surviving to pick 19 and St. Brown has 8%. The quarterback you get two rounds later is
> nearly as good; the receiver is not.
>
> Alternatives: Kyren Williams (RB, 1% survival), Jonathan Taylor (RB, 4%).

## Between picks

> **You:** Sync again. Who's best available?

> **Claude:** 18 picks in, you're up at 19. Best available: Kyren Williams, Jonathan
> Taylor, Josh Allen, Josh Jacobs.

> **You:** Compare Jonathan Taylor and Breece Hall.

> **Claude:** **Jonathan Taylor.** RB4 by projection (214 pts, 14.9/gm), consistency
> 0.62, startable in 61% of weeks.
>
> Hall projects slightly lower (195 pts) with similar consistency (0.60), but carries
> more injury risk given his history. Taylor's line ranks 7th in run block; Hall's is
> 19th. Taylor also draws the easier positional schedule.
>
> The case for Hall: better receiving role, which matters more if you're in full PPR.
> In half PPR that edge shrinks.

## Mid-draft

> **You:** I need a tight end. What's out there?

> **Claude:** Best available TEs: McBride (177 pts, consistency 0.60), Kittle (163, 0.62),
> LaPorta (155, 0.57).
>
> Kittle is the value — ADP 106 against a model rank of 28. Separation 3.39 yards with
> 2.44 YPRR, elite for the position. The caveat is age: he's past the tight end curve, so
> the projection already carries a decline adjustment.

> **You:** Any rookies worth taking here?

> **Claude:** Jeremiyah Love (RB, ARI, pick 3) is the only one close to your range —
> 239 projected points, ADP 38. Consistency 0.37, which is normal for a rookie and about
> half a proven starter's.
>
> Treat rookie projections as the widest error bars on the board. They're modelled from
> draft capital and landing spot, not from anything he's done in the NFL.

## When something goes wrong

> **You:** I typed CMC but it says he's still available.

> **Claude:** He resolved fine — `CMC` → Christian McCaffrey via the shorthand map. He
> shows available because no pick was recorded for him. Want me to record him now?

> **You:** Resolve these: JSN, ARSB, Jefferson

> **Claude:** JSN → Jaxon Smith-Njigba (initialism). ARSB → Amon-Ra St. Brown
> (initialism). Jefferson → **ambiguous (2): Justin Jefferson, Van Jefferson** — give me
> a first name.

## After

> **You:** How did my draft go?

> **Claude:** 16 picks: 6 RB, 6 WR, 2 QB, 2 TE. Projected starter points: 1,847.
>
> Strongest: your receiver room, three inside the top 20 by projection. Thinnest: tight
> end, where you waited until round 8 — reasonable given the position's replacement level
> in a 12-team league.
