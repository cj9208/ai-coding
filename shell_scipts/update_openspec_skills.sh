# git pull the lastest version of the OpenSpec skills
# the repo is located at folder openspec-skills
# then copy the skills to the folder .opencode/skills

function update_openspec_skills() {
  echo "Updating OpenSpec skills..."
  cd openspec-skills
  git pull
  cd ..
  cp -r openspec-skills/openspec-* .opencode/skills
  echo "OpenSpec skills updated!"
}

update_openspec_skills
