// Profile identity normalization shared by draft restore, session events, and
// persisted unread-marker ownership.
export function _profileMatchesActiveProfile(profile, activeProfile) {
  const eventName = (typeof profile === 'string' && profile.trim()) ? profile.trim() : 'default';
  const activeName = (typeof activeProfile === 'string' && activeProfile.trim()) ? activeProfile.trim() : 'default';
  if (eventName === activeName) return true;
  return eventName === 'default' && !!S.activeProfileIsDefault;
}

export function _sessionEventProfilesMatch(eventProfile, activeProfile) {
  if (!(typeof eventProfile === 'string' && eventProfile.trim())) return true;
  return _profileMatchesActiveProfile(eventProfile, activeProfile);
}
