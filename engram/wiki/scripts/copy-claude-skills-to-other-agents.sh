# Copy .claude/skills to other agents.
if [ -d .claude ] 
then
    if [ -d .junie ] 
    then
        rm -rf .junie/skills
        rm -rf .junie/agents
    fi
    cp -rf .claude/skills .junie/
    cp -rf .claude/agents .junie/
    if [ -d .agemts ] 
    then
        rm -rf .agents/skills
        rm -rf .agents/agents
    fi
    cp -rf .claude/skills .agents/
    cp -rf .claude/agents .agents/
    echo "Claude skills copied to .junie/ and .agents/"
else
    echo "Wrong directory; must be run from directory with .claude"
    exit 1
fi

