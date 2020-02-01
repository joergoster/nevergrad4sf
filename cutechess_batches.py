"""
Compute batches of chess games using cutechess.

The base functionality, provided by cutechess_local_batch essentially
calls cutechess with reasonable arguments and parses the output.
The more interesting version, cutechess_executor_batch, runs multiple
batches asynchronously, using an executor (which can be MPIPoolExecutor).
"""
from subprocess import Popen, PIPE
from scipy.stats import norm
import sys
import math
import random
import re
import json
import argparse
from mpi4py.futures import MPIPoolExecutor
from mpi4py import MPI
from concurrent.futures import as_completed
from threading import Lock


def elo(score):
    """ convert a score into Elo"""
    epsilon = 1e-6
    score = max(epsilon, min(1 - epsilon, score))
    return -400.0 * math.log10(1.0 / score - 1.0)


def calc_stats(result):
    """Given a list of "w" "l" "d", compute score, elo and LOS, with error estimates"""
    wld = [0, 0, 0]
    for r in result:
        if r == "w":
            wld[0] += 1
        if r == "l":
            wld[1] += 1
        if r == "d":
            wld[2] += 1

    games = sum(wld)
    score = (1.0 * wld[0] + 0.0 * wld[1] + 0.5 * wld[2]) / games
    devw = math.pow(1.0 - score, 2.0) * wld[0] / games
    devl = math.pow(0.0 - score, 2.0) * wld[1] / games
    devd = math.pow(0.5 - score, 2.0) * wld[2] / games
    stddev = math.sqrt(devw + devl + devd) / math.sqrt(games)

    if wld[0] != wld[1]:
        a = (wld[0] - wld[1]) / math.sqrt(wld[0] + wld[1])
    else:
        a = 0.0
    los = norm.cdf(a)

    return {
        "score": score,
        "score_error": 1.95716 * stddev,
        "Elo": elo(score),
        "Elo_error": (elo(score + 1.95716 * stddev) - elo(score - 1.95716 * stddev))
        / 2,
        "LOS": los,
    }


class CutechessLocalBatch:
    """Compute a batch of games using cutechess"""

    def __init__(
        self,
        cutechess="./cutechess-cli",
        stockfish="./stockfish",
        book="noob_3moves.epd",
        tc="10.0+1.0",
        rounds=100,
        concurrency=2,
    ):
        """Basic properties of the batch of games can be specified"""
        self.cutechess = cutechess
        self.stockfish = stockfish
        self.book = book
        self.tc = tc
        self.rounds = rounds
        self.concurrency = concurrency
        self.total_games = 2 * rounds

    def run(self, variables):
        """Run a batch of games returning a list  containing 'w' 'l' 'd' results

           The results are show from the point of view of test, which is the version that is
           setup using the options set using the variables.
        """

        # The engine whose parameters will be optimized
        fcp = "name=test cmd=%s" % self.stockfish

        # The reference engine
        scp = "name=base cmd=%s" % self.stockfish

        # Parse the parameters that should be optimized
        for name in variables:
            # Make sure the parameter value is numeric
            try:
                float(variables[name])
            except ValueError:
                sys.exit(
                    "invalid value for parameter %s: %s\n" % (argv[i], argv[i + 1])
                )

            initstr = "option.{name}={value}".format(name=name, value=variables[name])
            fcp += ' "%s"' % initstr

        extension = None
        m = re.compile("(pgn|epd)$").search(self.book)
        if m:
            extension = m.group(1)

        if not extension:
            sys.exit("books must have epd or pgn extension: %s" % self.book)

        cutechess_base_args = (
            "-games 2 -repeat "
            + " -openings file=%s format=%s order=random" % (self.book, extension)
            + " -draw movenumber=34 movecount=8 score=20 -resign movecount=3 score=400"
        )
        cutechess_args = (
            "-engine %s -engine %s -each proto=uci tc=%s option.Hash=1 -rounds %d -concurrency %d -srand %d"
            % (
                fcp,
                scp,
                self.tc,
                self.rounds,
                self.concurrency,
                random.SystemRandom().randint(0, 2 ** 31 - 1),
            )
        )
        command = "%s %s %s" % (self.cutechess, cutechess_base_args, cutechess_args)

        # Run cutechess-cli and wait for it to finish
        process = Popen(command, shell=True, stdout=PIPE)
        output = process.communicate()[0]
        if process.returncode != 0:
            sys.exit("failed to execute command: %s\n" % command)

        # Convert Cutechess-cli's result into W/L/D (putting game pairs together)
        lines = []
        for line in output.decode("utf-8").splitlines():
            if line.startswith("Finished game"):
                lines.append(line)

        lines.sort(key=lambda l: float(l.split()[2]))

        score = []
        for line in lines:
            if line.find(": 1-0") != -1:
                if line.find("test vs base") != -1:
                    score.append("w")
                if line.find("base vs test") != -1:
                    score.append("l")
            elif line.find(": 0-1") != -1:
                if line.find("test vs base") != -1:
                    score.append("l")
                if line.find("base vs test") != -1:
                    score.append("w")
            elif line.find(": 1/2-1/2") != -1:
                score.append("d")
            else:
                True
                # ignore for now.

        return score


class CutechessExecutorBatch:
    def __init__(
        self,
        cutechess="./cutechess-cli",
        stockfish="./stockfish",
        book="noob_3moves.epd",
        tc="10.0+0.1",
        rounds=2,
        concurrency=2,
        batches=1,
        executor=None,
    ):
        """Compute a batch of games using cutechess, specifying an executor

           The executor (e.g. MPIPoolExecutor) allows for concurrency, in evaluating batches
        """

        self.local_batch = CutechessLocalBatch(
            cutechess, stockfish, book, tc, rounds, concurrency
        )
        self.batches = batches
        self.total_games = self.batches * self.local_batch.total_games
        self.executor = executor

    def run(self, variables):
        """Run a batch of games returning a list  containing 'w' 'l' 'd' results

           The results are show from the point of view of test, which is the version that is
           setup using the options set using the variables.
        """
        score = []
        fs = []

        for i in range(0, self.batches):
            fs.append(self.executor.submit(self.local_batch.run, variables))

        for f in as_completed(fs):
            score = score + f.result()

        return score


# mpirun -np 3 python3 -m mpi4py.futures cutechess_batches.py
# will lauch 2 workers (1 master).
if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--stockfish",
        type=str,
        default="./stockfish",
        help="Name of the stockfish binary",
    )
    parser.add_argument(
        "--cutechess",
        type=str,
        default="./cutechess-cli",
        help="Name of the cutechess binary",
    )
    parser.add_argument(
        "--book",
        type=str,
        default="./noob_3moves.epd",
        help="opening book in epd or pgn fomat",
    )
    parser.add_argument(
        "-tc", "--tc", type=str, default="10.0+0.1", help="time control"
    )
    parser.add_argument(
        "-g",
        "--games_per_batch",
        type=int,
        default=5000,
        help="Number of games per evaluation point",
    )
    parser.add_argument(
        "-cc",
        "--cutechess_concurrency",
        type=int,
        default=8,
        help="Number of concurrent games per cutechess worker",
    )
    parser.add_argument(
        "--parameters",
        type=str,
        default="optimal.json",
        help="A dictionary containingthe parameters at which evaluation should happen",
    )
    args = parser.parse_args()

    workers = MPI.COMM_WORLD.Get_size() - 1

    with open(args.parameters, "r") as infile:
        variables = json.load(infile)

    print(
        "Starting evaluation (%d games) with %d workers for the parameter set %s"
        % (args.games_per_batch, workers, str(variables)),
        flush=True,
    )

    batch = CutechessExecutorBatch(
        cutechess=args.cutechess,
        stockfish=args.stockfish,
        book=args.book,
        tc=args.tc,
        rounds=((args.games_per_batch + 1) // 2 + workers - 1) // workers,
        concurrency=args.cutechess_concurrency,
        batches=workers,
        executor=MPIPoolExecutor(),
    )
    results = batch.run(variables)
    print(calc_stats(results))