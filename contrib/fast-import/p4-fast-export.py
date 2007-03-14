#!/usr/bin/env python
#
# p4-fast-export.py
#
# Author: Simon Hausmann <hausmann@kde.org>
# License: MIT <http://www.opensource.org/licenses/mit-license.php>
#
# TODO:
#       - support integrations (at least p4i)
#       - support p4 submit (hah!)
#       - emulate p4's delete behavior: if a directory becomes empty delete it. continue
#         with parent dir until non-empty dir is found.
#
import os, string, sys, time, os.path
import marshal, popen2, getopt, sha
from sets import Set;

dataCache = False
commandCache = False

silent = False
knownBranches = Set()
createdBranches = Set()
committedChanges = Set()
branch = "refs/heads/master"
globalPrefix = previousDepotPath = os.popen("git-repo-config --get p4.depotpath").read()
detectBranches = False
changesFile = ""
if len(globalPrefix) != 0:
    globalPrefix = globalPrefix[:-1]

try:
    opts, args = getopt.getopt(sys.argv[1:], "", [ "branch=", "detect-branches", "changesfile=", "silent", "known-branches=",
                                                   "cache", "command-cache" ])
except getopt.GetoptError:
    print "fixme, syntax error"
    sys.exit(1)

for o, a in opts:
    if o == "--branch":
        branch = "refs/heads/" + a
    elif o == "--detect-branches":
        detectBranches = True
    elif o == "--changesfile":
        changesFile = a
    elif o == "--silent":
        silent= True
    elif o == "--known-branches":
        for branch in open(a).readlines():
            knownBranches.add(branch[:-1])
    elif o == "--cache":
        dataCache = True
        commandCache = True
    elif o == "--command-cache":
        commandCache = True

if len(args) == 0 and len(globalPrefix) != 0:
    if not silent:
        print "[using previously specified depot path %s]" % globalPrefix
elif len(args) != 1:
    print "usage: %s //depot/path[@revRange]" % sys.argv[0]
    print "\n    example:"
    print "    %s //depot/my/project/ -- to import the current head"
    print "    %s //depot/my/project/@all -- to import everything"
    print "    %s //depot/my/project/@1,6 -- to import only from revision 1 to 6"
    print ""
    print "    (a ... is not needed in the path p4 specification, it's added implicitly)"
    print ""
    sys.exit(1)
else:
    if len(globalPrefix) != 0 and globalPrefix != args[0]:
        print "previous import used depot path %s and now %s was specified. this doesn't work!" % (globalPrefix, args[0])
        sys.exit(1)
    globalPrefix = args[0]

changeRange = ""
revision = ""
users = {}
initialParent = ""
lastChange = 0
initialTag = ""

if globalPrefix.find("@") != -1:
    atIdx = globalPrefix.index("@")
    changeRange = globalPrefix[atIdx:]
    if changeRange == "@all":
        changeRange = ""
    elif changeRange.find(",") == -1:
        revision = changeRange
        changeRange = ""
    globalPrefix = globalPrefix[0:atIdx]
elif globalPrefix.find("#") != -1:
    hashIdx = globalPrefix.index("#")
    revision = globalPrefix[hashIdx:]
    globalPrefix = globalPrefix[0:hashIdx]
elif len(previousDepotPath) == 0:
    revision = "#head"

if globalPrefix.endswith("..."):
    globalPrefix = globalPrefix[:-3]

if not globalPrefix.endswith("/"):
    globalPrefix += "/"

def p4File(depotPath):
    cacheKey = "/tmp/p4cache/data-" + sha.new(depotPath).hexdigest()

    data = 0
    try:
        if not dataCache:
            raise
        data = open(cacheKey, "rb").read()
    except:
        data = os.popen("p4 print -q \"%s\"" % depotPath, "rb").read()
        if dataCache:
            open(cacheKey, "wb").write(data)

    return data

def p4CmdList(cmd):
    fullCmd = "p4 -G %s" % cmd;

    cacheKey = sha.new(fullCmd).hexdigest()
    cacheKey = "/tmp/p4cache/cmd-" + cacheKey

    cached = True
    pipe = 0
    try:
        if not commandCache:
            raise
        pipe = open(cacheKey, "rb")
    except:
        cached = False
        pipe = os.popen(fullCmd, "rb")

    result = []
    try:
        while True:
            entry = marshal.load(pipe)
            result.append(entry)
    except EOFError:
        pass
    pipe.close()

    if not cached and commandCache:
        pipe = open(cacheKey, "wb")
        for r in result:
            marshal.dump(r, pipe)
        pipe.close()

    return result

def p4Cmd(cmd):
    list = p4CmdList(cmd)
    result = {}
    for entry in list:
        result.update(entry)
    return result;

def extractFilesFromCommit(commit):
    files = []
    fnum = 0
    while commit.has_key("depotFile%s" % fnum):
        path =  commit["depotFile%s" % fnum]
        if not path.startswith(globalPrefix):
#            if not silent:
#                print "\nchanged files: ignoring path %s outside of %s in change %s" % (path, globalPrefix, change)
            fnum = fnum + 1
            continue

        file = {}
        file["path"] = path
        file["rev"] = commit["rev%s" % fnum]
        file["action"] = commit["action%s" % fnum]
        file["type"] = commit["type%s" % fnum]
        files.append(file)
        fnum = fnum + 1
    return files

def isSubPathOf(first, second):
    if not first.startswith(second):
        return False
    if first == second:
        return True
    return first[len(second)] == "/"

def branchesForCommit(files):
    global knownBranches
    branches = Set()

    for file in files:
        relativePath = file["path"][len(globalPrefix):]
        # strip off the filename
        relativePath = relativePath[0:relativePath.rfind("/")]

#        if len(branches) == 0:
#            branches.add(relativePath)
#            knownBranches.add(relativePath)
#            continue

        ###### this needs more testing :)
        knownBranch = False
        for branch in branches:
            if relativePath == branch:
                knownBranch = True
                break
#            if relativePath.startswith(branch):
            if isSubPathOf(relativePath, branch):
                knownBranch = True
                break
#            if branch.startswith(relativePath):
            if isSubPathOf(branch, relativePath):
                branches.remove(branch)
                break

        if knownBranch:
            continue

        for branch in knownBranches:
            #if relativePath.startswith(branch):
            if isSubPathOf(relativePath, branch):
                if len(branches) == 0:
                    relativePath = branch
                else:
                    knownBranch = True
                break

        if knownBranch:
            continue

        branches.add(relativePath)
        knownBranches.add(relativePath)

    return branches

def findBranchParent(branchPrefix, files):
    for file in files:
        path = file["path"]
        if not path.startswith(branchPrefix):
            continue
        action = file["action"]
        if action != "integrate" and action != "branch":
            continue
        rev = file["rev"]
        depotPath = path + "#" + rev

        log = p4CmdList("filelog \"%s\"" % depotPath)
        if len(log) != 1:
            print "eek! I got confused by the filelog of %s" % depotPath
            sys.exit(1);

        log = log[0]
        if log["action0"] != action:
            print "eek! wrong action in filelog for %s : found %s, expected %s" % (depotPath, log["action0"], action)
            sys.exit(1);

        branchAction = log["how0,0"]
#        if branchAction == "branch into" or branchAction == "ignored":
#            continue # ignore for branching

        if not branchAction.endswith(" from"):
            continue # ignore for branching
#            print "eek! file %s was not branched from but instead: %s" % (depotPath, branchAction)
#            sys.exit(1);

        source = log["file0,0"]
        if source.startswith(branchPrefix):
            continue

        lastSourceRev = log["erev0,0"]

        sourceLog = p4CmdList("filelog -m 1 \"%s%s\"" % (source, lastSourceRev))
        if len(sourceLog) != 1:
            print "eek! I got confused by the source filelog of %s%s" % (source, lastSourceRev)
            sys.exit(1);
        sourceLog = sourceLog[0]

        relPath = source[len(globalPrefix):]
        # strip off the filename
        relPath = relPath[0:relPath.rfind("/")]

        for branch in knownBranches:
            if isSubPathOf(relPath, branch):
#                print "determined parent branch branch %s due to change in file %s" % (branch, source)
                return branch
#            else:
#                print "%s is not a subpath of branch %s" % (relPath, branch)

    return ""

def commit(details, files, branch, branchPrefix, parent, merged = ""):
    global users
    global lastChange
    global committedChanges

    epoch = details["time"]
    author = details["user"]

    gitStream.write("commit %s\n" % branch)
#    gitStream.write("mark :%s\n" % details["change"])
    committedChanges.add(int(details["change"]))
    committer = ""
    if author in users:
        committer = "%s %s %s" % (users[author], epoch, tz)
    else:
        committer = "%s <a@b> %s %s" % (author, epoch, tz)

    gitStream.write("committer %s\n" % committer)

    gitStream.write("data <<EOT\n")
    gitStream.write(details["desc"])
    gitStream.write("\n[ imported from %s; change %s ]\n" % (branchPrefix, details["change"]))
    gitStream.write("EOT\n\n")

    if len(parent) > 0:
        gitStream.write("from %s\n" % parent)

    if len(merged) > 0:
        gitStream.write("merge %s\n" % merged)

    for file in files:
        path = file["path"]
        if not path.startswith(branchPrefix):
#            if not silent:
#                print "\nchanged files: ignoring path %s outside of branch prefix %s in change %s" % (path, branchPrefix, details["change"])
            continue
        rev = file["rev"]
        depotPath = path + "#" + rev
        relPath = path[len(branchPrefix):]
        action = file["action"]

        if file["type"] == "apple":
            print "\nfile %s is a strange apple file that forks. Ignoring!" %s path
            continue

        if action == "delete":
            gitStream.write("D %s\n" % relPath)
        else:
            mode = 644
            if file["type"].startswith("x"):
                mode = 755

            data = p4File(depotPath)

            gitStream.write("M %s inline %s\n" % (mode, relPath))
            gitStream.write("data %s\n" % len(data))
            gitStream.write(data)
            gitStream.write("\n")

    gitStream.write("\n")

    lastChange = int(details["change"])

def extractFilesInCommitToBranch(files, branchPrefix):
    newFiles = []

    for file in files:
        path = file["path"]
        if path.startswith(branchPrefix):
            newFiles.append(file)

    return newFiles

def findBranchSourceHeuristic(files, branch, branchPrefix):
    for file in files:
        action = file["action"]
        if action != "integrate" and action != "branch":
            continue
        path = file["path"]
        rev = file["rev"]
        depotPath = path + "#" + rev

        log = p4CmdList("filelog \"%s\"" % depotPath)
        if len(log) != 1:
            print "eek! I got confused by the filelog of %s" % depotPath
            sys.exit(1);

        log = log[0]
        if log["action0"] != action:
            print "eek! wrong action in filelog for %s : found %s, expected %s" % (depotPath, log["action0"], action)
            sys.exit(1);

        branchAction = log["how0,0"]

        if not branchAction.endswith(" from"):
            continue # ignore for branching
#            print "eek! file %s was not branched from but instead: %s" % (depotPath, branchAction)
#            sys.exit(1);

        source = log["file0,0"]
        if source.startswith(branchPrefix):
            continue

        lastSourceRev = log["erev0,0"]

        sourceLog = p4CmdList("filelog -m 1 \"%s%s\"" % (source, lastSourceRev))
        if len(sourceLog) != 1:
            print "eek! I got confused by the source filelog of %s%s" % (source, lastSourceRev)
            sys.exit(1);
        sourceLog = sourceLog[0]

        relPath = source[len(globalPrefix):]
        # strip off the filename
        relPath = relPath[0:relPath.rfind("/")]

        for candidate in knownBranches:
            if isSubPathOf(relPath, candidate) and candidate != branch:
                return candidate

    return ""

def changeIsBranchMerge(sourceBranch, destinationBranch, change):
    sourceFiles = {}
    for file in p4CmdList("files %s...@%s" % (globalPrefix + sourceBranch + "/", change)):
        if file["action"] == "delete":
            continue
        sourceFiles[file["depotFile"]] = file

    destinationFiles = {}
    for file in p4CmdList("files %s...@%s" % (globalPrefix + destinationBranch + "/", change)):
        destinationFiles[file["depotFile"]] = file

    for fileName in sourceFiles.keys():
        integrations = []
        deleted = False
        integrationCount = 0
        for integration in p4CmdList("integrated \"%s\"" % fileName):
            toFile = integration["fromFile"] # yes, it's true, it's fromFile
            if not toFile in destinationFiles:
                continue
            destFile = destinationFiles[toFile]
            if destFile["action"] == "delete":
#                print "file %s has been deleted in %s" % (fileName, toFile)
                deleted = True
                break
            integrationCount += 1
            if integration["how"] == "branch from":
                continue

            if int(integration["change"]) == change:
                integrations.append(integration)
                continue
            if int(integration["change"]) > change:
                continue

            destRev = int(destFile["rev"])

            startRev = integration["startFromRev"][1:]
            if startRev == "none":
                startRev = 0
            else:
                startRev = int(startRev)

            endRev = integration["endFromRev"][1:]
            if endRev == "none":
                endRev = 0
            else:
                endRev = int(endRev)

            initialBranch = (destRev == 1 and integration["how"] != "branch into")
            inRange = (destRev >= startRev and destRev <= endRev)
            newer = (destRev > startRev and destRev > endRev)

            if initialBranch or inRange or newer:
                integrations.append(integration)

        if deleted:
            continue

        if len(integrations) == 0 and integrationCount > 1:
            print "file %s was not integrated from %s into %s" % (fileName, sourceBranch, destinationBranch)
            return False

    return True

def getUserMap():
    users = {}

    for output in p4CmdList("users"):
        if not output.has_key("User"):
            continue
        users[output["User"]] = output["FullName"] + " <" + output["Email"] + ">"
    return users

users = getUserMap()

if len(changeRange) == 0:
    try:
        sout, sin, serr = popen2.popen3("git-name-rev --tags `git-rev-parse %s`" % branch)
        output = sout.read()
        if output.endswith("\n"):
            output = output[:-1]
        tagIdx = output.index(" tags/p4/")
        caretIdx = output.find("^")
        endPos = len(output)
        if caretIdx != -1:
            endPos = caretIdx
        rev = int(output[tagIdx + 9 : endPos]) + 1
        changeRange = "@%s,#head" % rev
        initialParent = os.popen("git-rev-parse %s" % branch).read()[:-1]
        initialTag = "p4/%s" % (int(rev) - 1)
    except:
        pass

tz = - time.timezone / 36
tzsign = ("%s" % tz)[0]
if tzsign != '+' and tzsign != '-':
    tz = "+" + ("%s" % tz)

gitOutput, gitStream, gitError = popen2.popen3("git-fast-import")

if len(revision) > 0:
    print "Doing initial import of %s from revision %s" % (globalPrefix, revision)

    details = { "user" : "git perforce import user", "time" : int(time.time()) }
    details["desc"] = "Initial import of %s from the state at revision %s" % (globalPrefix, revision)
    details["change"] = revision
    newestRevision = 0

    fileCnt = 0
    for info in p4CmdList("files %s...%s" % (globalPrefix, revision)):
        change = int(info["change"])
        if change > newestRevision:
            newestRevision = change

        if info["action"] == "delete":
            continue

        for prop in [ "depotFile", "rev", "action", "type" ]:
            details["%s%s" % (prop, fileCnt)] = info[prop]

        fileCnt = fileCnt + 1

    details["change"] = newestRevision

    try:
        commit(details, extractFilesFromCommit(details), branch, globalPrefix)
    except:
        print gitError.read()

else:
    changes = []

    if len(changesFile) > 0:
        output = open(changesFile).readlines()
        changeSet = Set()
        for line in output:
            changeSet.add(int(line))

        for change in changeSet:
            changes.append(change)

        changes.sort()
    else:
        output = os.popen("p4 changes %s...%s" % (globalPrefix, changeRange)).readlines()

        for line in output:
            changeNum = line.split(" ")[1]
            changes.append(changeNum)

        changes.reverse()

    if len(changes) == 0:
        if not silent:
            print "no changes to import!"
        sys.exit(1)

    cnt = 1
    for change in changes:
        description = p4Cmd("describe %s" % change)

        if not silent:
            sys.stdout.write("\rimporting revision %s (%s%%)" % (change, cnt * 100 / len(changes)))
            sys.stdout.flush()
        cnt = cnt + 1

        try:
            files = extractFilesFromCommit(description)
            if detectBranches:
                for branch in branchesForCommit(files):
                    knownBranches.add(branch)
                    branchPrefix = globalPrefix + branch + "/"

                    filesForCommit = extractFilesInCommitToBranch(files, branchPrefix)

                    merged = ""
                    parent = ""
                    ########### remove cnt!!!
                    if branch not in createdBranches and cnt > 2:
                        createdBranches.add(branch)
                        parent = findBranchParent(branchPrefix, files)
                        if parent == branch:
                            parent = ""
    #                    elif len(parent) > 0:
    #                        print "%s branched off of %s" % (branch, parent)

                    if len(parent) == 0:
                        merged = findBranchSourceHeuristic(filesForCommit, branch, branchPrefix)
                        if len(merged) > 0:
                            print "change %s could be a merge from %s into %s" % (description["change"], merged, branch)
                            if not changeIsBranchMerge(merged, branch, int(description["change"])):
                                merged = ""

                    branch = "refs/heads/" + branch
                    if len(parent) > 0:
                        parent = "refs/heads/" + parent
                    if len(merged) > 0:
                        merged = "refs/heads/" + merged
                    commit(description, files, branch, branchPrefix, parent, merged)
            else:
                commit(description, filesForCommit, branch, globalPrefix, initialParent)
                initialParent = ""
        except IOError:
            print gitError.read()
            sys.exit(1)

if not silent:
    print ""

gitStream.write("reset refs/tags/p4/%s\n" % lastChange)
gitStream.write("from %s\n\n" % branch);


gitStream.close()
gitOutput.close()
gitError.close()

os.popen("git-repo-config p4.depotpath %s" % globalPrefix).read()
if len(initialTag) > 0:
    os.popen("git tag -d %s" % initialTag).read()

sys.exit(0)
